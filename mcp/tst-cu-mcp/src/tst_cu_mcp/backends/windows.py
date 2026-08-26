"""Windows backend: Win32 via ``ctypes`` for geometry and input, Pillow for pixels.

Coordinates are global **physical pixels** with a top-left origin, spanning the
virtual desktop (so a monitor left of or above the primary has negative
coordinates, exactly as on macOS). Physical pixels are the right choice here
because Windows has per-monitor DPI: there is no single "logical point" space
that covers a 150%-scaled laptop panel and a 100% external monitor at once.
Reporting real pixels keeps one coherent space for capture and input, and the
image-pixel mapping above this layer is proportional, so it never needs the
scale factor at all.

Adds no dependency: ``ctypes`` is stdlib and Pillow is already required for the
downscale/re-encode path shared with macOS.

Everything touching ``ctypes.wintypes`` is built lazily inside :func:`_win`,
because importing ``ctypes.wintypes`` fails outright on non-Windows hosts. The
pure logic — the key vocabulary, the coordinate normalisation, the UTF-16
splitting — stays at module scope so it is importable and testable anywhere.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import PurePath
from types import SimpleNamespace
from typing import Any

from tst_cu_mcp.backends.base import Rect
from tst_cu_mcp.displays import DisplayInfo
from tst_cu_mcp.focus import WindowInfo
from tst_cu_mcp.permissions import build_windows_report

# --- Win32 constants --------------------------------------------------------

MONITORINFOF_PRIMARY = 0x1

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120

#: ``SendInput`` maps the virtual desktop onto a fixed 0..65535 grid per axis.
ABSOLUTE_RANGE = 65535

# PER_MONITOR_AWARE_V2; the only awareness mode that reports true per-monitor DPI.
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
DEFAULT_DPI = 96

#: Least privilege that still yields a process image name, and the only one that
#: works across an integrity boundary.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_PROCESS_PATH = 32768

MOUSE_BUTTONS = ("left", "right")

# --- key vocabulary (pure) --------------------------------------------------

#: Modifier name -> virtual-key code.
#:
#: ``cmd``/``command`` deliberately map to Ctrl. Models carry a mac-shaped
#: shortcut vocabulary and will ask for ``cmd+c``; mapping it to Ctrl makes the
#: intent work, where refusing it would fail every copy, paste and save. Use
#: ``win``/``super`` when the Windows key is actually meant — that is why both
#: names exist rather than overloading one.
MODS: dict[str, int] = {
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "option": 0x12,
    "cmd": 0x11,
    "command": 0x11,
    "win": 0x5B,
    "super": 0x5B,
}

#: Modifiers that exist on macOS with no Windows equivalent. Refused by name so
#: the failure is a message rather than a silently dropped modifier.
UNSUPPORTED_MODS: dict[str, str] = {
    "fn": "the Fn modifier is macOS-only; Windows exposes no virtual key for it",
}

KEYCODES: dict[str, int] = {
    # letters
    **{chr(c): 0x41 + c - ord("a") for c in range(ord("a"), ord("z") + 1)},
    # digits
    **{str(d): 0x30 + d for d in range(10)},
    # named / control keys
    "return": 0x0D,
    "enter": 0x0D,
    "tab": 0x09,
    "space": 0x20,
    # macOS calls the leftward-erasing key "delete"; Windows calls it Backspace.
    # Both names resolve to Backspace so a mac-shaped combo behaves as intended,
    # and "forward_delete" is the key labelled Delete on a PC.
    "delete": 0x08,
    "backspace": 0x08,
    "forward_delete": 0x2E,
    "escape": 0x1B,
    "esc": 0x1B,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "insert": 0x2D,
    # The Windows key is both a modifier and a key worth pressing on its own:
    # tapping it opens Start, which is the normal way to launch anything. It
    # appears in MODS as well, and the two do not collide — `parse_key_combo`
    # resolves the final token against this table and earlier tokens against
    # MODS, so "win" presses it and "win+d" holds it. Found by trying to open
    # Start from a live session and getting "unknown key 'win'".
    "win": 0x5B,
    "super": 0x5B,
    # punctuation by name (OEM codes, US layout)
    "minus": 0xBD,
    "equal": 0xBB,
    "leftbracket": 0xDB,
    "rightbracket": 0xDD,
    "backslash": 0xDC,
    "semicolon": 0xBA,
    "quote": 0xDE,
    "comma": 0xBC,
    "period": 0xBE,
    "slash": 0xBF,
    "grave": 0xC0,
    # function keys
    **{f"f{n}": 0x70 + n - 1 for n in range(1, 13)},
}

#: Keys that must carry ``KEYEVENTF_EXTENDEDKEY`` to reach the right physical key.
EXTENDED_KEYS = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C})


def parse_key_combo(combo: str) -> tuple[tuple[int, ...], int]:
    """Parse ``"ctrl+shift+t"`` into ``(modifier_vks, base_vk)``.

    Unlike macOS, Windows modifiers are real key presses rather than a flags
    field, so the parsed form is an ordered tuple of virtual keys to hold down.
    Raises ``ValueError`` on any unknown or unsupported token.
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combo")

    *modifiers, base = parts
    mod_vks: list[int] = []
    for modifier in modifiers:
        if modifier in UNSUPPORTED_MODS:
            raise ValueError(f"unsupported modifier {modifier!r}: {UNSUPPORTED_MODS[modifier]}")
        if modifier not in MODS:
            raise ValueError(f"unknown modifier {modifier!r}; valid: {sorted(set(MODS))}")
        mod_vks.append(MODS[modifier])

    if base in UNSUPPORTED_MODS:
        raise ValueError(f"unsupported key {base!r}: {UNSUPPORTED_MODS[base]}")
    if base not in KEYCODES:
        raise ValueError(f"unknown key {base!r}")
    return tuple(mod_vks), KEYCODES[base]


def normalize_to_virtual_desktop(x: float, y: float, virtual: Rect) -> tuple[int, int]:
    """Map a global pixel coordinate onto ``SendInput``'s 0..65535 absolute grid.

    ``virtual`` is the virtual-desktop rectangle ``(x, y, width, height)``, whose
    origin is negative when a monitor sits left of or above the primary.

    The divisor is ``width - 1``, not ``width``: the grid is inclusive at both
    ends, so dividing by the width would make the final column unreachable and
    bias every coordinate slightly toward the origin.
    """
    vx, vy, vw, vh = virtual
    if vw <= 0 or vh <= 0:
        raise ValueError(f"virtual desktop has no area: {virtual}")

    nx = round((x - vx) * ABSOLUTE_RANGE / max(vw - 1, 1))
    ny = round((y - vy) * ABSOLUTE_RANGE / max(vh - 1, 1))
    return (
        max(0, min(ABSOLUTE_RANGE, nx)),
        max(0, min(ABSOLUTE_RANGE, ny)),
    )


def utf16_units(text: str) -> list[int]:
    """Split *text* into UTF-16 code units for ``KEYEVENTF_UNICODE``.

    ``SendInput`` carries one 16-bit unit per event, so characters outside the
    BMP (emoji, for instance) are sent as their two surrogate units in order.
    Returning units rather than characters is what makes that fall out for free.
    """
    raw = text.encode("utf-16-le")
    return [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]


def order_displays(
    monitors: list[tuple[int, int, int, int, int, bool, int]],
) -> list[DisplayInfo]:
    """Turn raw monitor tuples into indexed :class:`DisplayInfo`, primary first.

    ``EnumDisplayMonitors`` guarantees no ordering, but the model addresses
    displays by index, so the order has to be stable across calls: primary
    first, then top-to-bottom, left-to-right.
    """
    ordered = sorted(monitors, key=lambda m: (not m[5], m[2], m[1]))
    return [
        DisplayInfo(
            display_id=handle,
            index=index,
            x=left,
            y=top,
            width=width,
            height=height,
            scale=round(dpi / DEFAULT_DPI, 4),
            is_main=is_primary,
        )
        for index, (handle, left, top, width, height, is_primary, dpi) in enumerate(ordered)
    ]


# --- lazy Win32 plumbing ----------------------------------------------------

_STATE: dict[str, Any] = {}


def _win() -> Any:
    """Build the ctypes structures and prototypes once, on first use."""
    cached = _STATE.get("ns")
    if cached is not None:
        return cached

    import ctypes
    from ctypes import wintypes

    ulong_ptr = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = (
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        )

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = (
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        )

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = (
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        )

    class _InputUnion(ctypes.Union):
        _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = (("type", wintypes.DWORD), ("u", _InputUnion))

    class MONITORINFO(ctypes.Structure):
        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        )

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        shcore: Any = ctypes.WinDLL("shcore", use_last_error=True)
    except OSError:  # pragma: no cover - shcore ships with Windows 8.1+
        shcore = None
    try:
        shell32: Any = ctypes.WinDLL("shell32", use_last_error=True)
    except OSError:  # pragma: no cover
        shell32 = None
    try:
        kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:  # pragma: no cover - kernel32 is always present
        kernel32 = None

    if kernel32 is not None:
        # PROCESS_QUERY_LIMITED_INFORMATION is deliberate: it is the least
        # privilege that still yields an image name, and unlike
        # PROCESS_QUERY_INFORMATION it works against processes at a higher
        # integrity level. Reading the name of an elevated window's process is
        # exactly the case where the guard matters most.
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MONITORINFO))
    user32.GetMonitorInfoW.restype = wintypes.BOOL

    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.WindowFromPoint.argtypes = (wintypes.POINT,)
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    monitor_enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
    user32.EnumDisplayMonitors.argtypes = (
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        monitor_enum_proc,
        wintypes.LPARAM,
    )
    user32.EnumDisplayMonitors.restype = wintypes.BOOL

    ns = SimpleNamespace(
        ctypes=ctypes,
        wintypes=wintypes,
        user32=user32,
        shcore=shcore,
        shell32=shell32,
        kernel32=kernel32,
        INPUT=INPUT,
        MONITORINFO=MONITORINFO,
        MONITOR_ENUM_PROC=monitor_enum_proc,
    )
    _STATE["ns"] = ns
    return ns


def _ensure_dpi_aware() -> None:
    """Opt into per-monitor DPI awareness, once, before any geometry is read.

    Without this the OS lies to us: ``GetSystemMetrics`` and monitor rectangles
    come back in scaled coordinates, so a 150% display reports 1280x800 for a
    1920x1200 panel and every captured region and click lands short. Each call
    is a no-op once awareness is set, including when the host process set it
    first, which is why failures here are ignored rather than raised.
    """
    if _STATE.get("dpi_aware"):
        return
    ns = _win()

    if hasattr(ns.user32, "SetProcessDpiAwarenessContext"):
        ns.user32.SetProcessDpiAwarenessContext.argtypes = (ns.ctypes.c_void_p,)
        ns.user32.SetProcessDpiAwarenessContext.restype = ns.wintypes.BOOL
        if ns.user32.SetProcessDpiAwarenessContext(
            ns.ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        ):
            _STATE["dpi_aware"] = True
            return

    # 2 = PROCESS_PER_MONITOR_DPI_AWARE
    if (
        ns.shcore is not None
        and hasattr(ns.shcore, "SetProcessDpiAwareness")
        and ns.shcore.SetProcessDpiAwareness(2) == 0
    ):
        _STATE["dpi_aware"] = True
        return

    if hasattr(ns.user32, "SetProcessDPIAware"):
        ns.user32.SetProcessDPIAware()

    _STATE["dpi_aware"] = True


def _dpi_for_monitor(handle: int) -> int:
    """Effective DPI for a monitor handle, defaulting to 96 when unavailable.

    The default engages when ``shcore`` is absent (Windows 8 and older) or
    ``GetDpiForMonitor`` fails or reports zero for this handle — i.e. whenever
    the true DPI cannot be learned. Because ``order_displays`` derives scale
    from DPI/96, such a monitor then reports scale 1.0 even if actually HiDPI.
    Geometry and input are unaffected — rectangles come back in physical pixels
    once PER_MONITOR_AWARE_V2 is set, and nothing else reads the scale — so the
    consequence is limited to an understated density figure in metadata.
    """
    ns = _win()
    if ns.shcore is None or not hasattr(ns.shcore, "GetDpiForMonitor"):
        return DEFAULT_DPI
    dpi_x = ns.wintypes.UINT()
    dpi_y = ns.wintypes.UINT()
    # 0 = MDT_EFFECTIVE_DPI
    hresult = ns.shcore.GetDpiForMonitor(
        ns.wintypes.HANDLE(handle), 0, ns.ctypes.byref(dpi_x), ns.ctypes.byref(dpi_y)
    )
    if hresult != 0 or not dpi_x.value:
        return DEFAULT_DPI
    return int(dpi_x.value)


def virtual_desktop() -> Rect:
    """The virtual-desktop rectangle in physical pixels."""
    _ensure_dpi_aware()
    ns = _win()
    return (
        ns.user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        ns.user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        ns.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        ns.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def cursor_position() -> tuple[int, int]:
    """Current cursor position in global physical pixels."""
    _ensure_dpi_aware()
    ns = _win()
    point = ns.wintypes.POINT()
    if not ns.user32.GetCursorPos(ns.ctypes.byref(point)):
        raise RuntimeError("GetCursorPos failed")
    return (int(point.x), int(point.y))


def _process_name(pid: int) -> str:
    """Executable name for *pid*, or "" if it cannot be read.

    Returns the bare filename rather than the full path: the guard matches on it,
    and a full path would leak the user's directory layout into every response
    for no benefit. Fails soft — an unreadable name is a normal outcome for a
    protected process, not an error worth aborting a click over.
    """
    ns = _win()
    if ns.kernel32 is None:
        return ""

    handle = ns.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = ns.wintypes.DWORD(MAX_PROCESS_PATH)
        buffer = ns.ctypes.create_unicode_buffer(MAX_PROCESS_PATH)
        if not ns.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ns.ctypes.byref(size)):
            return ""
        return PurePath(buffer.value).name
    finally:
        ns.kernel32.CloseHandle(handle)


def foreground_window() -> WindowInfo:
    """The window currently in front, with its title, process and bounds."""
    _ensure_dpi_aware()
    ns = _win()

    hwnd = ns.user32.GetForegroundWindow()
    if not hwnd:
        # Legitimately happens: during a desktop switch, or while the secure
        # desktop is up. Reported as an empty window rather than raised, so a
        # guard refuses cleanly instead of the tool erroring.
        return WindowInfo(title="", process="", pid=0, x=0, y=0, width=0, height=0)

    length = ns.user32.GetWindowTextLengthW(hwnd)
    title = ""
    if length > 0:
        buffer = ns.ctypes.create_unicode_buffer(length + 1)
        ns.user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value

    pid = ns.wintypes.DWORD(0)
    ns.user32.GetWindowThreadProcessId(hwnd, ns.ctypes.byref(pid))

    rect = ns.wintypes.RECT()
    if ns.user32.GetWindowRect(hwnd, ns.ctypes.byref(rect)):
        x, y = int(rect.left), int(rect.top)
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
    else:  # pragma: no cover - only if the window dies mid-call
        x = y = width = height = 0

    return WindowInfo(
        title=title,
        process=_process_name(int(pid.value)),
        pid=int(pid.value),
        x=x,
        y=y,
        width=width,
        height=height,
    )


def window_at_point(x: float, y: float) -> dict[str, Any]:
    """HWND under a global pixel. Observe only — no cursor move."""
    ns = _win()
    point = ns.wintypes.POINT(round(x), round(y))
    hwnd = ns.user32.WindowFromPoint(point)
    if not hwnd:
        return {}

    class_buf = ns.ctypes.create_unicode_buffer(256)
    ns.user32.GetClassNameW(hwnd, class_buf, 256)
    class_name = class_buf.value

    length = ns.user32.GetWindowTextLengthW(hwnd)
    title = ""
    if length > 0:
        buffer = ns.ctypes.create_unicode_buffer(length + 1)
        ns.user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value

    rect = ns.wintypes.RECT()
    box = {"x": float(x), "y": float(y), "width": 1.0, "height": 1.0}
    if ns.user32.GetWindowRect(hwnd, ns.ctypes.byref(rect)):
        box = {
            "x": float(rect.left),
            "y": float(rect.top),
            "width": float(rect.right - rect.left),
            "height": float(rect.bottom - rect.top),
        }

    attributes: dict[str, str] = {"class": class_name}
    if title:
        attributes["title"] = title
    attributes["hwnd"] = str(int(hwnd))
    return {
        "xpath": None,
        "role": class_name or None,
        "attributes": attributes,
        "box": box,
        "styles": {},
    }


def is_elevated() -> bool:
    """True if this process runs elevated. Fails closed."""
    ns = _win()
    if ns.shell32 is None or not hasattr(ns.shell32, "IsUserAnAdmin"):
        return False
    try:
        return bool(ns.shell32.IsUserAnAdmin())
    except OSError:  # pragma: no cover
        return False


def _send(*inputs: Any) -> None:
    """Send a batch of INPUT records, raising if the OS accepted fewer than all.

    A short return means something swallowed the input — most often UIPI, when
    the foreground window runs at a higher integrity level than we do. Raising
    keeps that from looking like a successful click that simply did nothing.
    """
    ns = _win()
    count = len(inputs)
    array = (ns.INPUT * count)(*inputs)
    sent = ns.user32.SendInput(count, array, ns.ctypes.sizeof(ns.INPUT))
    if sent != count:
        error = ns.ctypes.get_last_error()
        raise RuntimeError(
            f"SendInput delivered {sent} of {count} events (last error {error}). "
            "A window running at a higher integrity level will silently discard "
            "synthetic input — see check_permissions for the UIPI limitation."
        )


def _mouse_input(flags: int, nx: int = 0, ny: int = 0, data: int = 0) -> Any:
    ns = _win()
    record = ns.INPUT(type=INPUT_MOUSE)
    record.mi.dx = nx
    record.mi.dy = ny
    record.mi.mouseData = data & 0xFFFFFFFF
    record.mi.dwFlags = flags
    record.mi.time = 0
    record.mi.dwExtraInfo = 0
    return record


def _key_input(vk: int, *, up: bool = False, unicode_unit: int | None = None) -> Any:
    ns = _win()
    record = ns.INPUT(type=INPUT_KEYBOARD)
    flags = KEYEVENTF_KEYUP if up else 0
    if unicode_unit is not None:
        record.ki.wVk = 0
        record.ki.wScan = unicode_unit
        flags |= KEYEVENTF_UNICODE
    else:
        record.ki.wVk = vk
        record.ki.wScan = 0
        if vk in EXTENDED_KEYS:
            flags |= KEYEVENTF_EXTENDEDKEY
    record.ki.dwFlags = flags
    record.ki.time = 0
    record.ki.dwExtraInfo = 0
    return record


class WindowsBackend:
    """Win32 eyes and hands."""

    name = "windows"

    # --- eyes ---------------------------------------------------------------

    def list_displays(self) -> list[DisplayInfo]:
        """Enumerate active monitors with their true per-monitor DPI."""
        _ensure_dpi_aware()
        ns = _win()
        raw: list[tuple[int, int, int, int, int, bool, int]] = []

        def _callback(handle: Any, _hdc: Any, _rect: Any, _param: Any) -> int:
            info = ns.MONITORINFO()
            info.cbSize = ns.ctypes.sizeof(ns.MONITORINFO)
            if ns.user32.GetMonitorInfoW(handle, ns.ctypes.byref(info)):
                area = info.rcMonitor
                raw.append(
                    (
                        int(handle),
                        int(area.left),
                        int(area.top),
                        int(area.right - area.left),
                        int(area.bottom - area.top),
                        bool(info.dwFlags & MONITORINFOF_PRIMARY),
                        _dpi_for_monitor(int(handle)),
                    )
                )
            return 1

        ns.user32.EnumDisplayMonitors(None, None, ns.MONITOR_ENUM_PROC(_callback), 0)
        return order_displays(raw)

    def capture_png(self, rect: Rect) -> bytes:
        """Capture a global pixel rectangle straight to memory as PNG.

        Never touches the filesystem — unlike the macOS path, which has to go
        through ``screencapture`` and a temp file.
        """
        _ensure_dpi_aware()
        from PIL import ImageGrab

        x, y, width, height = rect
        try:
            image = ImageGrab.grab(
                bbox=(x, y, x + width, y + height),
                all_screens=True,
            )
        except OSError as exc:
            raise RuntimeError(f"screen capture failed: {exc}") from exc

        buffer = BytesIO()
        image.save(buffer, format="PNG")
        raw = buffer.getvalue()
        if not raw:
            raise RuntimeError("screen capture produced no image")
        return raw

    # --- hands --------------------------------------------------------------

    def move_mouse(self, x: float, y: float) -> None:
        nx, ny = normalize_to_virtual_desktop(x, y, virtual_desktop())
        _send(
            _mouse_input(
                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                nx,
                ny,
            )
        )

    def click(self, x: float, y: float, button: str, count: int) -> None:
        self.move_mouse(x, y)
        down, up = (
            (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
            if button == "left"
            else (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)
        )
        # Windows infers double/triple clicks from timing and position rather
        # than from an explicit click-state field, so the pairs are sent as one
        # batch: a single SendInput call keeps them inside the double-click time.
        records = []
        for _ in range(count):
            records.append(_mouse_input(down))
            records.append(_mouse_input(up))
        _send(*records)

    def type_text(self, text: str) -> None:
        records = []
        for unit in utf16_units(text):
            records.append(_key_input(0, unicode_unit=unit))
            records.append(_key_input(0, up=True, unicode_unit=unit))
        if records:
            _send(*records)

    def parse_key_combo(self, combo: str) -> tuple[tuple[int, ...], int]:
        return parse_key_combo(combo)

    def press_keys(self, combo: str) -> None:
        mod_vks, base_vk = parse_key_combo(combo)
        records = [_key_input(vk) for vk in mod_vks]
        records.append(_key_input(base_vk))
        records.append(_key_input(base_vk, up=True))
        records.extend(_key_input(vk, up=True) for vk in reversed(mod_vks))
        _send(*records)

    def scroll(self, dx: int, dy: int) -> None:
        records = []
        if dy:
            records.append(_mouse_input(MOUSEEVENTF_WHEEL, data=dy * WHEEL_DELTA))
        if dx:
            records.append(_mouse_input(MOUSEEVENTF_HWHEEL, data=dx * WHEEL_DELTA))
        if records:
            _send(*records)

    # --- state read-back ----------------------------------------------------

    def cursor_position(self) -> tuple[int, int]:
        return cursor_position()

    def foreground_window(self) -> WindowInfo:
        return foreground_window()

    # --- environment --------------------------------------------------------

    def check_permissions(self, *, request: bool = False) -> dict[str, Any]:
        """Report the Windows situation: no gate to grant, two ways to fail quietly.

        ``request`` is accepted for interface symmetry and ignored — there is no
        prompt to raise.
        """
        return build_windows_report(elevated=is_elevated())

    def hit_test(self, x: float, y: float) -> dict[str, Any]:
        """UIA-adjacent window under a global pixel. Never moves the pointer."""
        return window_at_point(x, y)
