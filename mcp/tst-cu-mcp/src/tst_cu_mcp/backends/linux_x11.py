"""Lazy X11 / XRandR / XTest plumbing. Nothing here runs at import time.

OS handles are acquired inside helpers so this module imports on macOS and
Windows. That is what lets selection tests construct ``LinuxBackend`` anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tst_cu_mcp.backends.base import Rect
from tst_cu_mcp.focus import WindowInfo

CurrentTime = 0
XA_WINDOW = 33
XA_CARDINAL = 6
XA_STRING = 31
RR_CONNECTED = 0
ZPIXMAP = 2
BUTTON_LEFT = 1
BUTTON_RIGHT = 3
BUTTON_SCROLL_UP = 4
BUTTON_SCROLL_DOWN = 5
BUTTON_SCROLL_LEFT = 6
BUTTON_SCROLL_RIGHT = 7

_STATE: dict[str, Any] = {}


def _x11() -> Any:
    """Load libX11 / libXrandr / libXtst and bind prototypes once."""
    cached = _STATE.get("ns")
    if cached is not None:
        return cached

    import ctypes
    from ctypes import (
        POINTER,
        Structure,
        c_char_p,
        c_int,
        c_long,
        c_ubyte,
        c_uint,
        c_ulong,
        c_ushort,
        c_void_p,
    )

    class XRRScreenResources(Structure):
        _fields_ = (
            ("timestamp", c_ulong),
            ("configTimestamp", c_ulong),
            ("ncrtc", c_int),
            ("crtcs", POINTER(c_ulong)),
            ("noutput", c_int),
            ("outputs", POINTER(c_ulong)),
            ("nmode", c_int),
            ("modes", c_void_p),
        )

    class XRRCrtcInfo(Structure):
        _fields_ = (
            ("timestamp", c_ulong),
            ("x", c_int),
            ("y", c_int),
            ("width", c_uint),
            ("height", c_uint),
            ("mode", c_ulong),
        )

    class XRROutputInfo(Structure):
        _fields_ = (
            ("timestamp", c_ulong),
            ("crtc", c_ulong),
            ("name", c_char_p),
            ("nameLen", c_int),
            ("mm_width", c_ulong),
            ("mm_height", c_ulong),
            ("connection", c_ushort),
        )

    class XWindowAttributes(Structure):
        _fields_ = (
            ("x", c_int),
            ("y", c_int),
            ("width", c_int),
            ("height", c_int),
            ("border_width", c_int),
            ("depth", c_int),
            ("visual", c_void_p),
            ("root", c_ulong),
            ("c_class", c_int),
            ("bit_gravity", c_int),
            ("win_gravity", c_int),
            ("backing_store", c_int),
            ("backing_planes", c_ulong),
            ("backing_pixel", c_ulong),
            ("save_under", c_int),
            ("colormap", c_ulong),
            ("map_installed", c_int),
            ("map_state", c_int),
            ("all_event_masks", c_long),
            ("your_event_mask", c_long),
            ("do_not_propagate_mask", c_long),
            ("override_redirect", c_int),
            ("screen", c_void_p),
        )

    x11 = ctypes.CDLL("libX11.so.6")
    xrandr = ctypes.CDLL("libXrandr.so.2")
    xtst = ctypes.CDLL("libXtst.so.6")

    x11.XOpenDisplay.argtypes = [c_char_p]
    x11.XOpenDisplay.restype = c_void_p
    x11.XCloseDisplay.argtypes = [c_void_p]
    x11.XDefaultScreen.argtypes = [c_void_p]
    x11.XDefaultScreen.restype = c_int
    x11.XRootWindow.argtypes = [c_void_p, c_int]
    x11.XRootWindow.restype = c_ulong
    x11.XDisplayWidth.argtypes = [c_void_p, c_int]
    x11.XDisplayWidth.restype = c_int
    x11.XDisplayHeight.argtypes = [c_void_p, c_int]
    x11.XDisplayHeight.restype = c_int
    x11.XFlush.argtypes = [c_void_p]
    x11.XInternAtom.argtypes = [c_void_p, c_char_p, c_int]
    x11.XInternAtom.restype = c_ulong
    x11.XGetWindowProperty.argtypes = [
        c_void_p,
        c_ulong,
        c_ulong,
        c_long,
        c_long,
        c_int,
        c_ulong,
        POINTER(c_ulong),
        POINTER(c_int),
        POINTER(c_ulong),
        POINTER(c_ulong),
        POINTER(POINTER(c_ubyte)),
    ]
    x11.XGetWindowProperty.restype = c_int
    x11.XFree.argtypes = [c_void_p]
    x11.XGetWindowAttributes.argtypes = [c_void_p, c_ulong, POINTER(XWindowAttributes)]
    x11.XGetWindowAttributes.restype = c_int
    x11.XTranslateCoordinates.argtypes = [
        c_void_p,
        c_ulong,
        c_ulong,
        c_int,
        c_int,
        POINTER(c_int),
        POINTER(c_int),
        POINTER(c_ulong),
    ]
    x11.XTranslateCoordinates.restype = c_int
    x11.XQueryPointer.argtypes = [
        c_void_p,
        c_ulong,
        POINTER(c_ulong),
        POINTER(c_ulong),
        POINTER(c_int),
        POINTER(c_int),
        POINTER(c_int),
        POINTER(c_int),
        POINTER(c_uint),
    ]
    x11.XQueryPointer.restype = c_int
    x11.XKeysymToKeycode.argtypes = [c_void_p, c_ulong]
    x11.XKeysymToKeycode.restype = c_uint
    x11.XDefaultRootWindow.argtypes = [c_void_p]
    x11.XDefaultRootWindow.restype = c_ulong

    xrandr.XRRGetScreenResourcesCurrent.argtypes = [c_void_p, c_ulong]
    xrandr.XRRGetScreenResourcesCurrent.restype = POINTER(XRRScreenResources)
    xrandr.XRRFreeScreenResources.argtypes = [POINTER(XRRScreenResources)]
    xrandr.XRRGetCrtcInfo.argtypes = [c_void_p, POINTER(XRRScreenResources), c_ulong]
    xrandr.XRRGetCrtcInfo.restype = POINTER(XRRCrtcInfo)
    xrandr.XRRFreeCrtcInfo.argtypes = [POINTER(XRRCrtcInfo)]
    xrandr.XRRGetOutputInfo.argtypes = [c_void_p, POINTER(XRRScreenResources), c_ulong]
    xrandr.XRRGetOutputInfo.restype = POINTER(XRROutputInfo)
    xrandr.XRRFreeOutputInfo.argtypes = [POINTER(XRROutputInfo)]
    xrandr.XRRGetOutputPrimary.argtypes = [c_void_p, c_ulong]
    xrandr.XRRGetOutputPrimary.restype = c_ulong

    xtst.XTestQueryExtension.argtypes = [
        c_void_p,
        POINTER(c_int),
        POINTER(c_int),
        POINTER(c_int),
        POINTER(c_int),
    ]
    xtst.XTestQueryExtension.restype = c_int
    xtst.XTestFakeMotionEvent.argtypes = [c_void_p, c_int, c_int, c_int, c_ulong]
    xtst.XTestFakeButtonEvent.argtypes = [c_void_p, c_uint, c_int, c_ulong]
    xtst.XTestFakeKeyEvent.argtypes = [c_void_p, c_uint, c_int, c_ulong]

    ns = SimpleNamespace(
        ctypes=ctypes,
        x11=x11,
        xrandr=xrandr,
        xtst=xtst,
        XRRScreenResources=XRRScreenResources,
        XRRCrtcInfo=XRRCrtcInfo,
        XRROutputInfo=XRROutputInfo,
        XWindowAttributes=XWindowAttributes,
        POINTER=POINTER,
        c_int=c_int,
        c_uint=c_uint,
        c_ulong=c_ulong,
        c_ubyte=c_ubyte,
        byref=ctypes.byref,
    )
    _STATE["ns"] = ns
    return ns


def display() -> Any:
    """Open (and cache) the X connection. Raises if ``DISPLAY`` is unusable."""
    cached = _STATE.get("dpy")
    if cached is not None:
        return cached
    ns = _x11()
    name = os.environ.get("DISPLAY", "")
    dpy = ns.x11.XOpenDisplay(name.encode() if name else None)
    if not dpy:
        raise RuntimeError(
            "cannot open an X11 display. Set DISPLAY and use an X11 session. "
            "A Wayland session is not supported (TD-2002)."
        )
    _STATE["dpy"] = dpy
    return dpy


def root_window(dpy: Any | None = None) -> int:
    ns = _x11()
    handle = display() if dpy is None else dpy
    return int(ns.x11.XDefaultRootWindow(handle))


def xtest_available(dpy: Any | None = None) -> bool:
    ns = _x11()
    handle = display() if dpy is None else dpy
    ev = ns.c_int()
    err = ns.c_int()
    major = ns.c_int()
    minor = ns.c_int()
    return bool(
        ns.xtst.XTestQueryExtension(
            handle, ns.byref(ev), ns.byref(err), ns.byref(major), ns.byref(minor)
        )
    )


def probe_display() -> tuple[bool, bool]:
    """``(display_open, xtest_present)`` without raising."""
    try:
        dpy = display()
    except (OSError, RuntimeError):
        return False, False
    try:
        return True, xtest_available(dpy)
    except OSError:
        return True, False


def list_raw_displays() -> list[tuple[int, int, int, int, int, bool, float]]:
    """RandR outputs as ``(id, x, y, w, h, is_main, scale)``.

    Falls back to the default screen when RandR reports nothing. Negative
    origins are preserved — that is the multi-monitor case the suite pins.
    """
    ns = _x11()
    dpy = display()
    root = root_window(dpy)
    raw: list[tuple[int, int, int, int, int, bool, float]] = []
    try:
        resources = ns.xrandr.XRRGetScreenResourcesCurrent(dpy, root)
        if resources:
            primary = int(ns.xrandr.XRRGetOutputPrimary(dpy, root))
            try:
                for i in range(resources.contents.noutput):
                    output = int(resources.contents.outputs[i])
                    info = ns.xrandr.XRRGetOutputInfo(dpy, resources, output)
                    if not info:
                        continue
                    try:
                        if info.contents.connection != RR_CONNECTED or not info.contents.crtc:
                            continue
                        crtc = ns.xrandr.XRRGetCrtcInfo(dpy, resources, info.contents.crtc)
                        if not crtc:
                            continue
                        try:
                            if crtc.contents.mode == 0 or crtc.contents.width == 0:
                                continue
                            width = int(crtc.contents.width)
                            height = int(crtc.contents.height)
                            mm = int(info.contents.mm_width)
                            scale = round((25.4 * width / mm) / 96.0, 4) if mm > 0 else 1.0
                            raw.append(
                                (
                                    output,
                                    int(crtc.contents.x),
                                    int(crtc.contents.y),
                                    width,
                                    height,
                                    output == primary or (primary == 0 and not raw),
                                    scale,
                                )
                            )
                        finally:
                            ns.xrandr.XRRFreeCrtcInfo(crtc)
                    finally:
                        ns.xrandr.XRRFreeOutputInfo(info)
            finally:
                ns.xrandr.XRRFreeScreenResources(resources)
    except OSError:
        raw = []

    if raw:
        if not any(item[5] for item in raw):
            first = raw[0]
            raw[0] = (first[0], first[1], first[2], first[3], first[4], True, first[6])
        return raw

    screen = int(ns.x11.XDefaultScreen(dpy))
    return [
        (
            0,
            0,
            0,
            int(ns.x11.XDisplayWidth(dpy, screen)),
            int(ns.x11.XDisplayHeight(dpy, screen)),
            True,
            1.0,
        )
    ]


def virtual_desktop() -> Rect:
    displays = list_raw_displays()
    left = min(d[1] for d in displays)
    top = min(d[2] for d in displays)
    right = max(d[1] + d[3] for d in displays)
    bottom = max(d[2] + d[4] for d in displays)
    return (left, top, right - left, bottom - top)


def capture_png(rect: Rect) -> bytes:
    """Capture a global pixel rectangle to an in-memory PNG."""
    from io import BytesIO

    from PIL import ImageGrab

    x, y, width, height = rect
    if width <= 0 or height <= 0:
        raise RuntimeError(f"capture rectangle has no area: {rect}")
    try:
        image = ImageGrab.grab(
            bbox=(x, y, x + width, y + height),
            xdisplay=os.environ.get("DISPLAY") or None,
        )
    except OSError as exc:
        raise RuntimeError(f"screen capture failed: {exc}") from exc
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    raw = buffer.getvalue()
    if not raw:
        raise RuntimeError("screen capture produced no image")
    return raw


def cursor_position() -> tuple[int, int]:
    ns = _x11()
    dpy = display()
    root = root_window(dpy)
    root_ret = ns.c_ulong()
    child = ns.c_ulong()
    root_x = ns.c_int()
    root_y = ns.c_int()
    win_x = ns.c_int()
    win_y = ns.c_int()
    mask = ns.c_uint()
    if not ns.x11.XQueryPointer(
        dpy,
        root,
        ns.byref(root_ret),
        ns.byref(child),
        ns.byref(root_x),
        ns.byref(root_y),
        ns.byref(win_x),
        ns.byref(win_y),
        ns.byref(mask),
    ):
        raise RuntimeError("XQueryPointer failed")
    return (int(root_x.value), int(root_y.value))


def move_mouse(x: float, y: float) -> None:
    ns = _x11()
    dpy = display()
    ns.xtst.XTestFakeMotionEvent(dpy, -1, round(x), round(y), CurrentTime)
    ns.x11.XFlush(dpy)


def click_button(button: int, count: int) -> None:
    ns = _x11()
    dpy = display()
    for _ in range(count):
        ns.xtst.XTestFakeButtonEvent(dpy, button, 1, CurrentTime)
        ns.xtst.XTestFakeButtonEvent(dpy, button, 0, CurrentTime)
    ns.x11.XFlush(dpy)


def scroll_buttons(dx: int, dy: int) -> None:
    ns = _x11()
    dpy = display()
    for _ in range(abs(dy)):
        button = BUTTON_SCROLL_UP if dy > 0 else BUTTON_SCROLL_DOWN
        ns.xtst.XTestFakeButtonEvent(dpy, button, 1, CurrentTime)
        ns.xtst.XTestFakeButtonEvent(dpy, button, 0, CurrentTime)
    for _ in range(abs(dx)):
        button = BUTTON_SCROLL_RIGHT if dx > 0 else BUTTON_SCROLL_LEFT
        ns.xtst.XTestFakeButtonEvent(dpy, button, 1, CurrentTime)
        ns.xtst.XTestFakeButtonEvent(dpy, button, 0, CurrentTime)
    if dx or dy:
        ns.x11.XFlush(dpy)


def press_keysym(keysym: int, *, down: bool) -> None:
    ns = _x11()
    dpy = display()
    keycode = int(ns.x11.XKeysymToKeycode(dpy, keysym))
    if keycode == 0:
        raise RuntimeError(
            f"XKeysymToKeycode returned 0 for keysym 0x{keysym:x}; "
            "the current X keymap does not contain that key"
        )
    ns.xtst.XTestFakeKeyEvent(dpy, keycode, 1 if down else 0, CurrentTime)
    ns.x11.XFlush(dpy)


def _atom(dpy: Any, name: bytes) -> int:
    ns = _x11()
    return int(ns.x11.XInternAtom(dpy, name, 0))


def _property(dpy: Any, window: int, atom: int, req_type: int, length: int) -> bytes | None:
    ns = _x11()
    actual_type = ns.c_ulong()
    actual_format = ns.c_int()
    nitems = ns.c_ulong()
    bytes_after = ns.c_ulong()
    prop = ns.POINTER(ns.c_ubyte)()
    status = ns.x11.XGetWindowProperty(
        dpy,
        window,
        atom,
        0,
        length,
        0,
        req_type,
        ns.byref(actual_type),
        ns.byref(actual_format),
        ns.byref(nitems),
        ns.byref(bytes_after),
        ns.byref(prop),
    )
    if status != 0 or not prop:
        return None
    try:
        count = int(nitems.value) * max(int(actual_format.value) // 8, 1)
        return bytes(prop[:count])
    finally:
        ns.x11.XFree(prop)


def _process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    comm = Path("/proc") / str(pid) / "comm"
    try:
        return comm.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def foreground_window() -> WindowInfo:
    """EWMH active window, or an empty ``WindowInfo`` if nothing is focused."""
    ns = _x11()
    dpy = display()
    root = root_window(dpy)
    raw = _property(dpy, root, _atom(dpy, b"_NET_ACTIVE_WINDOW"), XA_WINDOW, 1)
    if not raw or len(raw) < 4:
        return WindowInfo(title="", process="", pid=0, x=0, y=0, width=0, height=0)
    window = int.from_bytes(raw[: ns.ctypes.sizeof(ns.c_ulong)], byteorder="little")
    if window == 0:
        return WindowInfo(title="", process="", pid=0, x=0, y=0, width=0, height=0)
    return _describe_window(dpy, window)


def window_at_point(x: float, y: float) -> dict[str, Any]:
    """Topmost EWMH client containing a global pixel. Never moves the pointer."""
    ns = _x11()
    dpy = display()
    root = root_window(dpy)
    raw = _property(dpy, root, _atom(dpy, b"_NET_CLIENT_LIST_STACKING"), XA_WINDOW, 4096)
    if not raw:
        raw = _property(dpy, root, _atom(dpy, b"_NET_CLIENT_LIST"), XA_WINDOW, 4096)
    if not raw:
        return {}
    size = ns.ctypes.sizeof(ns.c_ulong)
    windows = [
        int.from_bytes(raw[offset : offset + size], byteorder="little")
        for offset in range(0, len(raw) - size + 1, size)
    ]
    px, py = round(x), round(y)
    for window in reversed(windows):
        if window == 0:
            continue
        info = _describe_window(dpy, window)
        if info.width <= 0 or info.height <= 0:
            continue
        if info.x <= px < info.x + info.width and info.y <= py < info.y + info.height:
            attributes: dict[str, str] = {}
            if info.title:
                attributes["title"] = info.title
            if info.process:
                attributes["process"] = info.process
            return {
                "xpath": None,
                "role": "window",
                "attributes": attributes,
                "box": {
                    "x": float(info.x),
                    "y": float(info.y),
                    "width": float(info.width),
                    "height": float(info.height),
                },
                "styles": {},
            }
    return {}


def _describe_window(dpy: Any, window: int) -> WindowInfo:
    ns = _x11()
    root = root_window(dpy)
    title = ""
    utf8 = _atom(dpy, b"UTF8_STRING")
    name = _property(dpy, window, _atom(dpy, b"_NET_WM_NAME"), utf8, 4096)
    if name:
        title = name.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    elif legacy := _property(dpy, window, _atom(dpy, b"WM_NAME"), XA_STRING, 4096):
        title = legacy.split(b"\x00", 1)[0].decode("latin-1", errors="replace")

    pid = 0
    pid_raw = _property(dpy, window, _atom(dpy, b"_NET_WM_PID"), XA_CARDINAL, 1)
    if pid_raw:
        pid = int.from_bytes(pid_raw[:4], byteorder="little")

    attrs = ns.XWindowAttributes()
    x = y = width = height = 0
    if ns.x11.XGetWindowAttributes(dpy, window, ns.byref(attrs)):
        width = int(attrs.width)
        height = int(attrs.height)
        dest_x = ns.c_int()
        dest_y = ns.c_int()
        child = ns.c_ulong()
        if ns.x11.XTranslateCoordinates(
            dpy, window, root, 0, 0, ns.byref(dest_x), ns.byref(dest_y), ns.byref(child)
        ):
            x = int(dest_x.value)
            y = int(dest_y.value)

    return WindowInfo(
        title=title,
        process=_process_name(pid),
        pid=pid,
        x=x,
        y=y,
        width=width,
        height=height,
    )
