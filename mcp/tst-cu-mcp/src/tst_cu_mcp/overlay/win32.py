"""Windows rust ring: four click-through layered bars per display.

Imports ``ctypes.wintypes`` only inside helpers so this module loads on
macOS and Linux. An overlay problem degrades to a no-op.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from tst_cu_mcp.overlay.style import (
    CORE_WIDTH_PX,
    INSET_PX,
    LIGHT_RUST_RGB,
    PULSE_MAX,
    PULSE_MIN,
    PULSE_PERIOD_SECONDS,
    TICK_SECONDS,
)

WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_POPUP = 0x80000000
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
SW_HIDE = 0
LWA_ALPHA = 0x02
LWA_COLORKEY = 0x01


def _pulse_alpha(now: float) -> float:
    phase = (now % PULSE_PERIOD_SECONDS) / PULSE_PERIOD_SECONDS
    return PULSE_MIN + (PULSE_MAX - PULSE_MIN) * (0.5 + 0.5 * math.sin(phase * 2 * math.pi))


class Win32Overlay:
    """Layered, click-through bars around each monitor."""

    name = "win32"

    def __init__(self) -> None:
        self.degraded = False
        self._lock = threading.Lock()
        self._hwnds: list[int] = []
        self._visible = False
        self._grab_depth = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ns: Any = None

    def begin_session(self) -> None:
        self.activity()

    def end_session(self) -> None:
        with self._lock:
            self._visible = False
            self._apply_map(False)

    def activity(self) -> None:
        from tst_cu_mcp import safety

        if safety.killswitch_engaged():
            self.notify_blocked()
            return
        with self._lock:
            if self.degraded:
                return
            if not self._ensure():
                return
            self._visible = True
            if self._grab_depth == 0:
                self._apply_map(True)

    def notify_blocked(self) -> None:
        self.end_session()

    @contextmanager
    def grab_hidden(self) -> Iterator[None]:
        with self._lock:
            self._grab_depth += 1
            self._apply_map(False)
        try:
            yield
        finally:
            with self._lock:
                self._grab_depth = max(0, self._grab_depth - 1)
                if self._grab_depth == 0 and self._visible:
                    self._apply_map(True)

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
        with self._lock:
            self._destroy()
            self.degraded = True

    def _ensure(self) -> bool:
        if self.degraded:
            return False
        if self._hwnds:
            return True
        try:
            self._boot()
        except Exception:
            self.degraded = True
            self._destroy()
            return False
        return bool(self._hwnds)

    def _boot(self) -> None:
        from tst_cu_mcp.backends import get_backend

        ns = _load_win()
        self._ns = ns
        color = ns.user32.GetSysColor(0)  # unused fallback
        rust = (LIGHT_RUST_RGB[0]) | (LIGHT_RUST_RGB[1] << 8) | (LIGHT_RUST_RGB[2] << 16)
        _ = color
        thickness = CORE_WIDTH_PX
        inset = INSET_PX
        for display in get_backend().list_displays():
            x, y, w, h = display.x, display.y, display.width, display.height
            bars = (
                (x + inset, y + inset, max(1, w - 2 * inset), thickness),
                (x + inset, y + h - inset - thickness, max(1, w - 2 * inset), thickness),
                (x + inset, y + inset, thickness, max(1, h - 2 * inset)),
                (x + w - inset - thickness, y + inset, thickness, max(1, h - 2 * inset)),
            )
            for bx, by, bw, bh in bars:
                self._hwnds.append(_create_bar(ns, bx, by, bw, bh, rust))
        self._stop.clear()
        self._thread = threading.Thread(target=self._pulse, name="cu-overlay-pulse", daemon=True)
        self._thread.start()

    def _pulse(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            with self._lock:
                if self.degraded or not self._visible or self._grab_depth or self._ns is None:
                    continue
                try:
                    alpha = int(_pulse_alpha(time.monotonic()) * 255)
                    for hwnd in self._hwnds:
                        self._ns.user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)
                except Exception:
                    self.degraded = True
                    return

    def _apply_map(self, mapped: bool) -> None:
        if self._ns is None:
            return
        flag = SWP_SHOWWINDOW if mapped else SWP_HIDEWINDOW
        for hwnd in self._hwnds:
            self._ns.user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOACTIVATE | 0x0001 | 0x0002 | flag
            )

    def _destroy(self) -> None:
        ns, hwnds = self._ns, self._hwnds
        self._ns = None
        self._hwnds = []
        if ns is None:
            return
        for hwnd in hwnds:
            with suppress(Exception):
                ns.user32.DestroyWindow(hwnd)


def _create_bar(ns: Any, x: int, y: int, w: int, h: int, colorref: int) -> int:
    ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    hwnd = ns.user32.CreateWindowExW(
        ex,
        "STATIC",
        "",
        WS_POPUP,
        int(x),
        int(y),
        int(w),
        int(h),
        None,
        None,
        None,
        None,
    )
    if not hwnd:
        raise RuntimeError("CreateWindowExW failed")
    brush = ns.gdi32.CreateSolidBrush(colorref)
    ns.user32.SetClassLongPtrW(hwnd, -10, brush)  # GCLP_HBRBACKGROUND
    ns.user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
    return int(hwnd)


_WIN_NS: Any = None


def _load_win() -> Any:
    global _WIN_NS
    if _WIN_NS is not None:
        return _WIN_NS
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.SetLayeredWindowAttributes.argtypes = [
        wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD
    ]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
    _WIN_NS = ctypes.SimpleNamespace(user32=user32, gdi32=gdi32, ctypes=ctypes, wintypes=wintypes)
    return _WIN_NS
