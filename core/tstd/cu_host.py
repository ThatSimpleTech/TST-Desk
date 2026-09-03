"""Computer-use actuation fallback inside the TST Desk sidecar.

Checkout Python is not the host app. Capture and input APIs from that
interpreter are attributed to TST Desk but never attach to the helper.
The packaged host (``tst-desk``) owns TCC: it binds
``{data_dir}/cu-agent.sock`` and runs CoreGraphics there. Darwin
``tst-cu-mcp`` is only a client of that socket.

This module still serves the same protocol when the host socket is
absent (CLI ``tstd``, tests). ``--cu-agent`` stdin mode remains for
tests. ``--cu-capture`` is a one-shot.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

from .logging import get_logger

log = get_logger("tstd.cu_host")

SOCK_ENV = "TST_CU_AGENT_SOCK"
SOCK_NAME = "cu-agent.sock"

# CGEventFlags / virtual keycodes (US), same ABI as tst-cu-mcp Darwin.
_MODS: dict[str, int] = {
    "cmd": 1 << 20,
    "command": 1 << 20,
    "shift": 1 << 17,
    "alt": 1 << 19,
    "option": 1 << 19,
    "ctrl": 1 << 18,
    "control": 1 << 18,
    "fn": 1 << 23,
}
_KEYS: dict[str, int] = {
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "6": 22,
    "5": 23,
    "9": 25,
    "7": 26,
    "minus": 27,
    "8": 28,
    "0": 29,
    "rightbracket": 30,
    "o": 31,
    "u": 32,
    "leftbracket": 33,
    "i": 34,
    "p": 35,
    "return": 36,
    "enter": 36,
    "l": 37,
    "j": 38,
    "quote": 39,
    "k": 40,
    "semicolon": 41,
    "backslash": 42,
    "comma": 43,
    "slash": 44,
    "n": 45,
    "m": 46,
    "period": 47,
    "tab": 48,
    "space": 49,
    "grave": 50,
    "delete": 51,
    "backspace": 51,
    "escape": 53,
    "esc": 53,
    "home": 115,
    "pageup": 116,
    "forward_delete": 117,
    "end": 119,
    "pagedown": 121,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "equal": 24,
}

_screen_capable: bool | None = None
_cu_server: asyncio.AbstractServer | None = None


def run_cu_mcp() -> int:
    """Serve ``tst-cu-mcp`` on stdio, in this (host) process."""
    os.environ["TST_CU_MCP_HOST"] = "1"
    if not _prepare_cu_imports():
        sys.stderr.write("tstd --cu-mcp: tst-cu-mcp is not on this machine\n")
        return 2
    from tst_cu_mcp import main

    main()
    return 0


def run_cu_capture(argv: list[str]) -> int:
    """``tstd --cu-capture x y w h`` → PNG on stdout. No logs on stdout."""
    os.environ["TST_CU_MCP_HOST"] = "1"
    os.environ["TST_CU_CAPTURE"] = "1"
    try:
        rest = argv[argv.index("--cu-capture") + 1 :]
        x, y, w, h = (int(part) for part in rest[:4])
    except (ValueError, IndexError):
        sys.stderr.write("tstd --cu-capture x y w h\n")
        return 2
    png = _ctypes_capture_png(x, y, w, h)
    if not png:
        sys.stderr.write("tstd --cu-capture: no image\n")
        return 1
    sys.stdout.buffer.write(png)
    return 0


def run_cu_agent() -> int:
    """Stdin protocol for tests. Production uses :func:`start_cu_socket`."""
    os.environ["TST_CU_MCP_HOST"] = "1"
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        line = stdin.readline()
        if not line:
            return 0
        text = line.decode("utf-8", "replace").strip()
        header, blob = dispatch(text)
        stdout.write(header)
        if blob:
            stdout.write(blob)
        stdout.flush()
        if not text or text == "quit":
            return 0


def sock_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / SOCK_NAME


async def start_cu_socket(data_dir: str | Path) -> asyncio.AbstractServer | None:
    """Serve CU commands on a Unix socket in *data_dir*. Darwin only.

    If the TST Desk host already bound the socket, leave it alone so
    actuation stays in the process System Settings names **TST Desk**.
    """
    global _cu_server
    if sys.platform != "darwin":
        return None
    path = sock_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if _socket_is_live(path):
        os.environ[SOCK_ENV] = str(path)
        return None
    if path.exists():
        path.unlink()
    try:
        server = await asyncio.start_unix_server(_cu_client, path=str(path))
    except OSError as exc:
        # AF_UNIX paths cap at 104 bytes on macOS. A deep data dir must not
        # keep the daemon from booting; actuation falls back to the driver.
        log.warning(
            "cu socket unavailable",
            extra={"extra_fields": {"path": str(path), "error": str(exc)}},
        )
        return None
    os.chmod(path, 0o600)
    os.environ[SOCK_ENV] = str(path)
    _cu_server = server
    return server


async def stop_cu_socket(data_dir: str | Path | None = None) -> None:
    """Close a socket this process owns. Do not unlink the host's socket."""
    global _cu_server
    server = _cu_server
    _cu_server = None
    owned = server is not None
    if server is not None:
        server.close()
        await server.wait_closed()
    if not owned:
        return
    path = sock_path(data_dir) if data_dir is not None else None
    env = os.environ.get(SOCK_ENV, "").strip()
    for candidate in (path, Path(env) if env else None):
        if candidate is not None:
            with contextlib.suppress(OSError):
                candidate.unlink()
    os.environ.pop(SOCK_ENV, None)


def _socket_is_live(path: Path) -> bool:
    """True when something is accepting on this Unix socket."""
    try:
        if not path.exists():
            return False
    except OSError:
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.25)
    try:
        sock.connect(str(path))
    except OSError:
        return False
    finally:
        sock.close()
    return True


async def _cu_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            raw = await asyncio.wait_for(reader.readline(), timeout=120)
            if not raw:
                return
            text = raw.decode("utf-8", "replace").strip()
            header, blob = await asyncio.to_thread(dispatch, text)
            writer.write(header)
            if blob:
                writer.write(blob)
            await writer.drain()
            if not text or text == "quit":
                return
    except (TimeoutError, ConnectionResetError, BrokenPipeError, OSError):
        return
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


def dispatch(text: str) -> tuple[bytes, bytes | None]:
    """Handle one CU command. Returns (header_line_with_nl, optional body)."""
    if not text or text == "quit":
        return b"ok\n", None
    if text == "permissions" or text == "permissions request":
        # Sidecar must not CGRequest: that dialog is attributed to TST Desk
        # and never sticks. The host handles ``permissions request``.
        return json.dumps(_permissions_dict()).encode("utf-8") + b"\n", None
    if text.startswith("capture "):
        try:
            x, y, w, h = (int(part) for part in text.split()[1:5])
        except ValueError:
            return b"err\n", None
        png = _ctypes_capture_png(x, y, w, h)
        if not png:
            return b"err\n", None
        global _screen_capable
        _screen_capable = True
        return f"png {len(png)}\n".encode("ascii"), png
    if text.startswith("move "):
        try:
            px, py = (float(part) for part in text.split()[1:3])
        except ValueError:
            return b"err\n", None
        return (b"ok\n" if _mouse_move(px, py) else b"err\n"), None
    if text.startswith("click "):
        parts = text.split()
        try:
            px, py = float(parts[1]), float(parts[2])
            button = parts[3] if len(parts) > 3 else "left"
            count = int(parts[4]) if len(parts) > 4 else 1
        except (ValueError, IndexError):
            return b"err\n", None
        return (b"ok\n" if _mouse_click(px, py, button, count) else b"err\n"), None
    if text.startswith("key "):
        combo = text[4:].strip()
        return (b"ok\n" if _press_combo(combo) else b"err\n"), None
    if text.startswith("scroll "):
        try:
            dx, dy = (int(part) for part in text.split()[1:3])
        except ValueError:
            return b"err\n", None
        return (b"ok\n" if _scroll(dx, dy) else b"err\n"), None
    if text.startswith("type "):
        try:
            payload = base64.b64decode(text[5:].strip().encode("ascii"))
            typed = payload.decode("utf-8")
        except (ValueError, UnicodeError):
            return b"err\n", None
        return (b"ok\n" if _type_text(typed) else b"err\n"), None
    return b"err\n", None


def _permissions_dict() -> dict[str, object]:
    screen = _screen_is_capable()
    access = _ax_trusted_ctypes()
    preflight = _cg_preflight_ctypes()
    return {
        "platform": "macos",
        "screen_recording": {
            "granted": screen,
            "required_for": "capturing the screen (screenshot / vision)",
        },
        "accessibility": {
            "granted": access,
            "required_for": "controlling the mouse and keyboard",
        },
        "all_granted": screen and access,
        "actuation_path": "daemon",
        "process_trusted": {
            "screen_recording": preflight,
            "accessibility": access,
        },
        "host_caveat": (
            "macOS grants these permissions to TST Desk, not a helper Python. "
            "Enable TST Desk in System Settings, then fully Quit (Cmd+Q). If "
            "Settings already shows TST Desk ON, the grant belongs to an older "
            "build: use Reset grants in TST Desk → Settings → Computer use."
        ),
        # Only the Tauri host can run tccutil for the bundle (TD-4823).
        "reset_supported": False,
    }


def _screen_is_capable() -> bool:
    global _screen_capable
    if _screen_capable is True:
        return True
    # Do not capture in the probe. CGWindowListCreateImage re-shows the
    # Screen Recording dialog on every permissions check.
    return _cg_preflight_ctypes()


def _cg() -> Any | None:
    if sys.platform != "darwin":
        return None
    import ctypes

    try:
        lib = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    except OSError:
        return None
    return lib


def _mouse_move(x: float, y: float) -> bool:
    return _post_mouse(5, x, y, 0, 0)  # kCGEventMouseMoved


def _mouse_click(x: float, y: float, button: str, count: int) -> bool:
    if button == "right":
        down, up, btn = 3, 4, 1
    else:
        down, up, btn = 1, 2, 0
    n = max(1, min(int(count), 3))
    for i in range(n):
        if not _post_mouse(down, x, y, btn, i + 1):
            return False
        if not _post_mouse(up, x, y, btn, i + 1):
            return False
    return True


def _post_mouse(kind: int, x: float, y: float, button: int, click: int) -> bool:
    import ctypes
    from ctypes import c_double, c_int32, c_uint32, c_void_p

    lib = _cg()
    if lib is None:
        return False

    class CGPoint(ctypes.Structure):
        _fields_ = [("x", c_double), ("y", c_double)]

    lib.CGEventCreateMouseEvent.restype = c_void_p
    lib.CGEventCreateMouseEvent.argtypes = [c_void_p, c_uint32, CGPoint, c_uint32]
    lib.CGEventSetIntegerValueField.argtypes = [c_void_p, c_uint32, c_int32]
    lib.CGEventPost.argtypes = [c_uint32, c_void_p]
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFRelease.argtypes = [c_void_p]
    event = lib.CGEventCreateMouseEvent(None, kind, CGPoint(x, y), button)
    if not event:
        return False
    if click:
        lib.CGEventSetIntegerValueField(event, 1, click)  # kCGMouseEventClickState
    lib.CGEventPost(0, event)  # kCGHIDEventTap
    cf.CFRelease(event)
    return True


def _press_combo(combo: str) -> bool:
    try:
        flags, keycode = _parse_combo(combo)
    except ValueError:
        return False
    import ctypes
    from ctypes import c_bool, c_uint16, c_uint32, c_uint64, c_void_p

    lib = _cg()
    if lib is None:
        return False
    lib.CGEventCreateKeyboardEvent.restype = c_void_p
    lib.CGEventCreateKeyboardEvent.argtypes = [c_void_p, c_uint16, c_bool]
    lib.CGEventSetFlags.argtypes = [c_void_p, c_uint64]
    lib.CGEventPost.argtypes = [c_uint32, c_void_p]
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFRelease.argtypes = [c_void_p]
    for pressed in (True, False):
        event = lib.CGEventCreateKeyboardEvent(None, keycode, pressed)
        if not event:
            return False
        if flags:
            lib.CGEventSetFlags(event, flags)
        lib.CGEventPost(0, event)
        cf.CFRelease(event)
    return True


def _parse_combo(combo: str) -> tuple[int, int]:
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combo")
    *modifiers, base = parts
    flags = 0
    for modifier in modifiers:
        if modifier not in _MODS:
            raise ValueError(f"unknown modifier {modifier!r}")
        flags |= _MODS[modifier]
    if base not in _KEYS:
        raise ValueError(f"unknown key {base!r}")
    return flags, _KEYS[base]


def _scroll(dx: int, dy: int) -> bool:
    import ctypes
    from ctypes import c_int32, c_uint32, c_void_p

    lib = _cg()
    if lib is None:
        return False
    lib.CGEventCreateScrollWheelEvent.restype = c_void_p
    lib.CGEventPost.argtypes = [c_uint32, c_void_p]
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFRelease.argtypes = [c_void_p]
    # source, units=line(0), wheelCount=2, wheel1=dy, wheel2=dx
    lib.CGEventCreateScrollWheelEvent.argtypes = [
        c_void_p,
        c_uint32,
        c_uint32,
        c_int32,
        c_int32,
    ]
    event = lib.CGEventCreateScrollWheelEvent(None, 0, 2, int(dy), int(dx))
    if not event:
        return False
    lib.CGEventPost(0, event)
    cf.CFRelease(event)
    return True


def _type_text(text: str) -> bool:
    import ctypes
    from ctypes import POINTER, c_bool, c_uint16, c_uint32, c_void_p

    lib = _cg()
    if lib is None:
        return False
    lib.CGEventCreateKeyboardEvent.restype = c_void_p
    lib.CGEventCreateKeyboardEvent.argtypes = [c_void_p, c_uint16, c_bool]
    lib.CGEventKeyboardSetUnicodeString.argtypes = [c_void_p, c_uint32, POINTER(c_uint16)]
    lib.CGEventPost.argtypes = [c_uint32, c_void_p]
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFRelease.argtypes = [c_void_p]
    for char in text:
        encoded = char.encode("utf-16-le")
        n = max(1, len(encoded) // 2)
        buf = (c_uint16 * n).from_buffer_copy(encoded.ljust(n * 2, b"\x00"))
        for pressed in (True, False):
            event = lib.CGEventCreateKeyboardEvent(None, 0, pressed)
            if not event:
                return False
            lib.CGEventKeyboardSetUnicodeString(event, n, buf)
            lib.CGEventPost(0, event)
            cf.CFRelease(event)
    return True


def _cg_preflight_ctypes() -> bool:
    if sys.platform != "darwin":
        return False
    import ctypes

    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(cg.CGPreflightScreenCaptureAccess())
    except (OSError, AttributeError):
        return False


def _ax_trusted_ctypes() -> bool:
    if sys.platform != "darwin":
        return False
    import ctypes

    try:
        app_services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        app_services.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(app_services.AXIsProcessTrusted())
    except (OSError, AttributeError):
        return False


def _ctypes_capture_png(x: int, y: int, w: int, h: int) -> bytes | None:
    """CoreGraphics capture via system frameworks. No pyobjc (sidecar is 3.11)."""
    if sys.platform != "darwin" or w <= 0 or h <= 0:
        return None
    import ctypes
    from ctypes import POINTER, c_char, c_double, c_int32, c_uint32, c_void_p

    class CGRect(ctypes.Structure):
        _fields_ = [
            ("x", c_double),
            ("y", c_double),
            ("width", c_double),
            ("height", c_double),
        ]

    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        imageio = ctypes.CDLL("/System/Library/Frameworks/ImageIO.framework/ImageIO")
    except OSError:
        return None

    k_cf_string_encoding_utf8 = 0x08000100
    cf.CFStringCreateWithCString.restype = c_void_p
    cf.CFStringCreateWithCString.argtypes = [c_void_p, ctypes.c_char_p, c_uint32]
    cf.CFDataCreateMutable.restype = c_void_p
    cf.CFDataCreateMutable.argtypes = [c_void_p, ctypes.c_long]
    cf.CFDataGetLength.restype = ctypes.c_long
    cf.CFDataGetLength.argtypes = [c_void_p]
    cf.CFDataGetBytePtr.restype = POINTER(c_char)
    cf.CFDataGetBytePtr.argtypes = [c_void_p]
    cf.CFRelease.argtypes = [c_void_p]

    cg.CGWindowListCreateImage.restype = c_void_p
    cg.CGWindowListCreateImage.argtypes = [CGRect, c_uint32, c_uint32, c_uint32]
    cg.CGImageGetWidth.restype = ctypes.c_size_t
    cg.CGImageGetWidth.argtypes = [c_void_p]
    cg.CGImageGetHeight.restype = ctypes.c_size_t
    cg.CGImageGetHeight.argtypes = [c_void_p]

    imageio.CGImageDestinationCreateWithData.restype = c_void_p
    imageio.CGImageDestinationCreateWithData.argtypes = [
        c_void_p,
        c_void_p,
        c_uint32,
        c_void_p,
    ]
    imageio.CGImageDestinationAddImage.argtypes = [c_void_p, c_void_p, c_void_p]
    imageio.CGImageDestinationFinalize.restype = c_int32
    imageio.CGImageDestinationFinalize.argtypes = [c_void_p]

    image = cg.CGWindowListCreateImage(
        CGRect(float(x), float(y), float(w), float(h)),
        1,  # kCGWindowListOptionOnScreenOnly
        0,  # kCGNullWindowID
        0,  # kCGWindowImageDefault
    )
    if not image:
        return None
    if cg.CGImageGetWidth(image) < 1 or cg.CGImageGetHeight(image) < 1:
        cf.CFRelease(image)
        return None
    uti = cf.CFStringCreateWithCString(None, b"public.png", k_cf_string_encoding_utf8)
    data = cf.CFDataCreateMutable(None, 0)
    dest = None
    if data and uti:
        dest = imageio.CGImageDestinationCreateWithData(data, uti, 1, None)
    png: bytes | None = None
    try:
        if not dest:
            return None
        imageio.CGImageDestinationAddImage(dest, image, None)
        if not imageio.CGImageDestinationFinalize(dest):
            return None
        length = cf.CFDataGetLength(data)
        ptr = cf.CFDataGetBytePtr(data)
        if not ptr or length < 8:
            return None
        png = ctypes.string_at(ptr, length)
        if not png.startswith(b"\x89PNG"):
            return None
        return png
    finally:
        if dest:
            cf.CFRelease(dest)
        if data:
            cf.CFRelease(data)
        if uti:
            cf.CFRelease(uti)
        cf.CFRelease(image)


def _prepare_cu_imports() -> bool:
    """Put checkout ``tst-cu-mcp`` (src + venv) on ``sys.path``."""
    src = _checkout_cu_src()
    if src is None:
        return False
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    site = _venv_site(src.parent)
    if site is not None and str(site) not in sys.path:
        sys.path.insert(0, str(site))
    try:
        import tst_cu_mcp  # noqa: F401
    except ImportError:
        return False
    return True


def _checkout_cu_src() -> Path | None:
    suffix = Path("mcp") / "tst-cu-mcp" / "src"
    roots: list[Path] = []
    here = Path(__file__).resolve()
    roots.extend(here.parents[i] for i in range(1, min(6, len(here.parents))))
    roots.append(Path.cwd())
    roots.extend(Path.cwd().parents[:4])
    roots.append(Path.home() / "Documents" / "TST-Desk")
    roots.append(Path.home() / "TST-Desk")
    seen: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        candidate = resolved / suffix
        if (candidate / "tst_cu_mcp" / "__init__.py").is_file():
            return candidate
    return None


def _venv_site(package_root: Path) -> Path | None:
    lib = package_root / ".venv" / "lib"
    if not lib.is_dir():
        return None
    needle = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for child in lib.iterdir():
        if child.name.startswith(needle):
            site = child / "site-packages"
            if site.is_dir():
                return site
    return None
