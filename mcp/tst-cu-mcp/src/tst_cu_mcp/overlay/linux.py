"""X11 rust ring: four click-through bars per display, session-scoped.

Wayland is refused at resolution (``overlay_enabled``). An overlay
problem degrades to a no-op and never breaks actuation.
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

_XA_CARDINAL = 6
_PROP_MODE_REPLACE = 0
_SHAPE_INPUT = 2
_SHAPE_SET = 0
_UNSORTED = 0


def _pulse_alpha(now: float) -> float:
    phase = (now % PULSE_PERIOD_SECONDS) / PULSE_PERIOD_SECONDS
    return PULSE_MIN + (PULSE_MAX - PULSE_MIN) * (0.5 + 0.5 * math.sin(phase * 2 * math.pi))


class LinuxOverlay:
    """Override-redirect bars around each X11 display."""

    name = "linux"

    def __init__(self) -> None:
        self.degraded = False
        self._lock = threading.Lock()
        self._dpy: Any = None
        self._windows: list[int] = []
        self._visible = False
        self._grab_depth = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._x11: Any = None

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
        if self._dpy is not None:
            return True
        try:
            self._boot()
        except Exception:
            self.degraded = True
            self._destroy()
            return False
        return self._dpy is not None

    def _boot(self) -> None:
        from tst_cu_mcp.backends import get_backend

        x11 = _load_x11()
        dpy = x11.XOpenDisplay(None)
        if not dpy:
            raise RuntimeError("XOpenDisplay failed")
        self._x11 = x11
        self._dpy = dpy
        rust = _rgb_pixel(x11, dpy, LIGHT_RUST_RGB)
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
                self._windows.append(_create_bar(x11, dpy, bx, by, bw, bh, rust))
        x11.XFlush(dpy)
        self._stop.clear()
        self._thread = threading.Thread(target=self._pulse, name="cu-overlay-pulse", daemon=True)
        self._thread.start()

    def _pulse(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            with self._lock:
                if self.degraded or self._dpy is None or not self._visible or self._grab_depth:
                    continue
                try:
                    alpha = _pulse_alpha(time.monotonic())
                    _set_opacity(self._x11, self._dpy, self._windows, alpha)
                    self._x11.XFlush(self._dpy)
                except Exception:
                    self.degraded = True
                    return

    def _apply_map(self, mapped: bool) -> None:
        if self._dpy is None or self._x11 is None:
            return
        for win in self._windows:
            if mapped:
                self._x11.XMapRaised(self._dpy, win)
            else:
                self._x11.XUnmapWindow(self._dpy, win)
        self._x11.XFlush(self._dpy)

    def _destroy(self) -> None:
        x11, dpy, windows = self._x11, self._dpy, self._windows
        self._x11 = None
        self._dpy = None
        self._windows = []
        if x11 is None or dpy is None:
            return
        for win in windows:
            with suppress(Exception):
                x11.XDestroyWindow(dpy, win)
        with suppress(Exception):
            x11.XCloseDisplay(dpy)


def _rgb_pixel(x11: Any, dpy: Any, rgb: tuple[int, int, int]) -> int:
    import ctypes

    screen = x11.XDefaultScreen(dpy)
    cmap = x11.XDefaultColormap(dpy, screen)
    color = x11.XColor()
    color.red = rgb[0] * 257
    color.green = rgb[1] * 257
    color.blue = rgb[2] * 257
    color.flags = 7
    if x11.XAllocColor(dpy, cmap, ctypes.byref(color)) == 0:
        return x11.XBlackPixel(dpy, screen)
    return int(color.pixel)


def _create_bar(x11: Any, dpy: Any, x: int, y: int, w: int, h: int, pixel: int) -> int:
    import ctypes

    screen = x11.XDefaultScreen(dpy)
    root = x11.XRootWindow(dpy, screen)
    attrs = x11.XSetWindowAttributes()
    attrs.override_redirect = 1
    attrs.background_pixel = pixel
    attrs.border_pixel = pixel
    mask = 0x0200 | 0x0002 | 0x0008  # CWOverrideRedirect | CWBackPixel | CWBorderPixel
    win = x11.XCreateWindow(
        dpy,
        root,
        int(x),
        int(y),
        int(w),
        int(h),
        0,
        x11.XDefaultDepth(dpy, screen),
        1,
        x11.XDefaultVisual(dpy, screen),
        mask,
        ctypes.byref(attrs),
    )
    _shape_no_input(x11, dpy, win)
    return int(win)


def _shape_no_input(x11: Any, dpy: Any, win: int) -> None:
    with suppress(Exception):
        xext = _load_xext()
        xext.XShapeCombineRectangles(dpy, win, _SHAPE_INPUT, 0, 0, None, 0, _SHAPE_SET, _UNSORTED)


def _set_opacity(x11: Any, dpy: Any, windows: list[int], alpha: float) -> None:
    import ctypes

    atom = x11.XInternAtom(dpy, b"_NET_WM_WINDOW_OPACITY", 0)
    value = ctypes.c_ulong(int(max(0.0, min(1.0, alpha)) * 0xFFFFFFFF))
    for win in windows:
        x11.XChangeProperty(
            dpy, win, atom, _XA_CARDINAL, 32, _PROP_MODE_REPLACE, ctypes.byref(value), 1
        )


_X11_NS: Any = None
_XEXT_NS: Any = None


def _load_x11() -> Any:
    global _X11_NS
    if _X11_NS is not None:
        return _X11_NS
    import ctypes
    from ctypes import Structure, c_char_p, c_int, c_long, c_ulong, c_ushort, c_void_p

    class XColor(Structure):
        _fields_ = (
            ("pixel", c_ulong),
            ("red", c_ushort),
            ("green", c_ushort),
            ("blue", c_ushort),
            ("flags", ctypes.c_ubyte),
            ("pad", ctypes.c_ubyte),
        )

    class XSetWindowAttributes(Structure):
        _fields_ = (
            ("background_pixmap", c_ulong),
            ("background_pixel", c_ulong),
            ("border_pixmap", c_ulong),
            ("border_pixel", c_ulong),
            ("bit_gravity", c_int),
            ("win_gravity", c_int),
            ("backing_store", c_int),
            ("backing_planes", c_ulong),
            ("backing_pixel", c_ulong),
            ("save_under", c_int),
            ("event_mask", c_long),
            ("do_not_propagate_mask", c_long),
            ("override_redirect", c_int),
            ("colormap", c_ulong),
            ("cursor", c_ulong),
        )

    lib = ctypes.cdll.LoadLibrary("libX11.so.6")
    lib.XOpenDisplay.restype = c_void_p
    lib.XOpenDisplay.argtypes = [c_char_p]
    lib.XDefaultScreen.argtypes = [c_void_p]
    lib.XDefaultScreen.restype = c_int
    lib.XRootWindow.argtypes = [c_void_p, c_int]
    lib.XRootWindow.restype = c_ulong
    lib.XDefaultColormap.argtypes = [c_void_p, c_int]
    lib.XDefaultColormap.restype = c_ulong
    lib.XDefaultDepth.argtypes = [c_void_p, c_int]
    lib.XDefaultDepth.restype = c_int
    lib.XDefaultVisual.argtypes = [c_void_p, c_int]
    lib.XDefaultVisual.restype = c_void_p
    lib.XBlackPixel.argtypes = [c_void_p, c_int]
    lib.XBlackPixel.restype = c_ulong
    lib.XAllocColor.argtypes = [c_void_p, c_ulong, c_void_p]
    lib.XAllocColor.restype = c_int
    lib.XCreateWindow.restype = c_ulong
    lib.XInternAtom.restype = c_ulong
    lib.XInternAtom.argtypes = [c_void_p, c_char_p, c_int]
    lib.XChangeProperty.argtypes = [
        c_void_p, c_ulong, c_ulong, c_ulong, c_int, c_int, c_void_p, c_int
    ]
    lib.XMapRaised.argtypes = [c_void_p, c_ulong]
    lib.XUnmapWindow.argtypes = [c_void_p, c_ulong]
    lib.XDestroyWindow.argtypes = [c_void_p, c_ulong]
    lib.XCloseDisplay.argtypes = [c_void_p]
    lib.XFlush.argtypes = [c_void_p]
    lib.XColor = XColor
    lib.XSetWindowAttributes = XSetWindowAttributes
    _X11_NS = lib
    return lib


def _load_xext() -> Any:
    global _XEXT_NS
    if _XEXT_NS is not None:
        return _XEXT_NS
    import ctypes
    from ctypes import c_int, c_ulong, c_void_p

    lib = ctypes.cdll.LoadLibrary("libXext.so.6")
    lib.XShapeCombineRectangles.argtypes = [
        c_void_p, c_ulong, c_int, c_int, c_int, c_void_p, c_int, c_int, c_int
    ]
    _XEXT_NS = lib
    return lib
