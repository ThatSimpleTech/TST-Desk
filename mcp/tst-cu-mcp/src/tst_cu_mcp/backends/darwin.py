"""macOS backend: CoreGraphics for geometry, input, and capture.

This is the original implementation, moved behind :class:`~tst_cu_mcp.backends.base.Backend`
without behavioural change. Coordinates are global **logical points** with a
top-left origin — the space shared by ``CGDisplayBounds``, ``screencapture -R``
and ``CGEvent`` mouse posting.

Every pyobjc import is function-local. That is not style: it keeps this module
importable on Windows, where pyobjc is not installed at all (its dependency
markers exclude it), so backend selection and the key vocabulary stay testable
from either host.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from tst_cu_mcp.backends.base import Rect
from tst_cu_mcp.displays import DisplayInfo
from tst_cu_mcp.focus import WindowInfo
from tst_cu_mcp.permissions import ACCESSIBILITY, NO_HOST_FIX, SCREEN_RECORDING, build_report

# Each TCC prompt is raised at most once. Grok (and other clients) pass
# request=true on every check_permissions call; CGRequestScreenCaptureAccess
# then re-shows the dialog even after the user granted the *host* app, because
# this interpreter is not that binary. The stamp survives process restarts.
PROMPT_STAMP_DIR = Path.home() / ".tst-cu-mcp" / "macos-tcc-prompted"
_prompted_this_process: set[str] = set()
SOCK_ENV = "TST_CU_AGENT_SOCK"
_TEXT_TIMEOUT = 3.0
_CAPTURE_TIMEOUT = 8.0

MAX_DISPLAYS = 16

# CGEventFlags modifier masks (ABI-stable bit positions).
MODS: dict[str, int] = {
    "cmd": 1 << 20,
    "command": 1 << 20,
    "shift": 1 << 17,
    "alt": 1 << 19,
    "option": 1 << 19,
    "ctrl": 1 << 18,
    "control": 1 << 18,
    "fn": 1 << 23,
}

# Virtual key codes (US layout).
KEYCODES: dict[str, int] = {
    # letters
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
    "o": 31,
    "u": 32,
    "i": 34,
    "p": 35,
    "l": 37,
    "j": 38,
    "k": 40,
    "n": 45,
    "m": 46,
    # digits
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "5": 23,
    "6": 22,
    "7": 26,
    "8": 28,
    "9": 25,
    "0": 29,
    # named / control keys
    "return": 36,
    "enter": 36,
    "tab": 48,
    "space": 49,
    "delete": 51,
    "backspace": 51,
    "forward_delete": 117,
    "escape": 53,
    "esc": 53,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "pagedown": 121,
    # punctuation by name
    "minus": 27,
    "equal": 24,
    "leftbracket": 33,
    "rightbracket": 30,
    "backslash": 42,
    "semicolon": 41,
    "quote": 39,
    "comma": 43,
    "period": 47,
    "slash": 44,
    "grave": 50,
}

MOUSE_BUTTONS = ("left", "right")


def parse_key_combo(combo: str) -> tuple[int, int]:
    """Parse ``"cmd+shift+t"`` into ``(modifier_flags, keycode)``.

    Tokens are separated by ``+``. All but the last are modifiers; the last is
    the base key. Raises ``ValueError`` on any unknown token.
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combo")

    *modifiers, base = parts
    flags = 0
    for modifier in modifiers:
        if modifier not in MODS:
            raise ValueError(f"unknown modifier {modifier!r}; valid: {sorted(set(MODS))}")
        flags |= MODS[modifier]

    if base not in KEYCODES:
        raise ValueError(f"unknown key {base!r}")
    return flags, KEYCODES[base]


class DarwinBackend:
    """CoreGraphics eyes and hands."""

    name = "darwin"

    # --- eyes ---------------------------------------------------------------

    def list_displays(self) -> list[DisplayInfo]:
        """Enumerate active displays via CoreGraphics."""
        from Quartz import (
            CGDisplayBounds,
            CGDisplayCopyDisplayMode,
            CGDisplayModeGetPixelWidth,
            CGDisplayModeGetWidth,
            CGGetActiveDisplayList,
            CGMainDisplayID,
        )

        err, ids, count = CGGetActiveDisplayList(MAX_DISPLAYS, None, None)
        if err != 0 or not ids:
            return []

        main_id = CGMainDisplayID()
        displays: list[DisplayInfo] = []
        for index, display_id in enumerate(list(ids)[:count]):
            bounds = CGDisplayBounds(display_id)

            # Fallback scale: engages only when the display mode cannot be read
            # (CGDisplayCopyDisplayMode returns None, or the point width is 0).
            # Consequence: a HiDPI panel then reports scale 1.0. Nothing
            # geometric consumes it — bounds stay in points and the image-pixel
            # mapping is proportional over the captured region — so the cost is
            # confined to an understated density figure in screen metadata.
            scale = 1.0
            mode = CGDisplayCopyDisplayMode(display_id)
            if mode is not None:
                pixel_w = CGDisplayModeGetPixelWidth(mode)
                point_w = CGDisplayModeGetWidth(mode)
                if point_w:
                    scale = round(pixel_w / point_w, 4)

            displays.append(
                DisplayInfo(
                    display_id=int(display_id),
                    index=index,
                    x=round(bounds.origin.x),
                    y=round(bounds.origin.y),
                    width=round(bounds.size.width),
                    height=round(bounds.size.height),
                    scale=scale,
                    is_main=bool(display_id == main_id),
                )
            )
        return displays

    def capture_png(self, rect: Rect) -> bytes:
        """Capture a global-point rectangle as PNG bytes.

        Capture APIs prompt as the *host app* when this interpreter is not
        that binary. Checkout Python must never call them. The packaged
        TST Desk host (``tst-desk``) owns capture over ``cu-agent.sock``.
        """
        if not _is_host_identity():
            raw = _capture_via_agent(rect)
            if raw:
                return raw
        elif _cg_preflight() or _is_host_identity():
            raw = _cg_capture_png(rect)
            if raw:
                return raw
            if _cg_preflight():
                return self._capture_via_screencapture(rect)
        raise RuntimeError(
            "Screen Recording is not available to the TST Desk host. Enable TST Desk "
            "in System Settings > Privacy & Security > Screen Recording, then fully "
            "Quit (Cmd+Q) and reopen it. If System Settings already shows TST Desk "
            "ON, the grant belongs to an older build of the app: ask the user to "
            "click Reset grants in TST Desk → Settings → Computer use (or run "
            "`tccutil reset ScreenCapture com.thatsimpletech.tstdesk`), relaunch, "
            "and allow again. Do not click Allow on a re-raised prompt expecting it "
            "to stick, and do not retry this action until check_permissions reports "
            "it granted."
        )

    def _capture_via_screencapture(self, rect: Rect) -> bytes:
        """CLI fallback. Only called when this process already holds TCC."""
        fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="tstcu-")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            self._run_screencapture(rect, tmp)
            raw = tmp.read_bytes()
        finally:
            tmp.unlink(missing_ok=True)

        if not raw:
            raise RuntimeError(
                "screencapture produced no image; Screen Recording permission may be "
                "missing — call check_permissions."
            )
        return raw

    @staticmethod
    def _run_screencapture(rect: Rect, out_path: Path) -> None:
        """Invoke the ``screencapture`` CLI for a global-point rectangle (no shell)."""
        x, y, w, h = rect
        cmd = ["screencapture", "-x", "-t", "png", f"-R{x},{y},{w},{h}", str(out_path)]
        proc = subprocess.run(cmd, capture_output=True, timeout=15, check=False)
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace")[:200]
            raise RuntimeError(
                f"screencapture failed (rc={proc.returncode}): {detail}. This usually "
                "means Screen Recording permission is missing for the host app — call "
                "check_permissions."
            )

    # --- hands --------------------------------------------------------------

    def move_mouse(self, x: float, y: float) -> None:
        if not _is_host_identity():
            _require_ok(_cu_transact(f"move {x} {y}"), "move")
            return
        _require_accessibility("move")
        import Quartz

        event = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def click(self, x: float, y: float, button: str, count: int) -> None:
        if not _is_host_identity():
            _require_ok(_cu_transact(f"click {x} {y} {button} {count}"), "click")
            return
        _require_accessibility("click")
        import Quartz

        if button == "left":
            down, up, btn = (
                Quartz.kCGEventLeftMouseDown,
                Quartz.kCGEventLeftMouseUp,
                Quartz.kCGMouseButtonLeft,
            )
        else:
            down, up, btn = (
                Quartz.kCGEventRightMouseDown,
                Quartz.kCGEventRightMouseUp,
                Quartz.kCGMouseButtonRight,
            )

        for i in range(count):
            for phase in (down, up):
                event = Quartz.CGEventCreateMouseEvent(None, phase, (x, y), btn)
                # Click-state makes the OS register i+1 as a single/double/triple click.
                Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, i + 1)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def type_text(self, text: str) -> None:
        if not _is_host_identity():
            import base64

            token = base64.b64encode(text.encode("utf-8")).decode("ascii")
            _require_ok(_cu_transact(f"type {token}"), "type")
            return
        _require_accessibility("type")
        import Quartz

        # Local import: no cycle — input_control pulls in this package for
        # get_backend. The count is shared with the MAX_TEXT_LEN validation
        # there: one definition of "how many keyboard events a string costs".
        from ..input_control import utf16_length

        for char in text:
            # The length argument counts UTF-16 units, not characters — see
            # utf16_length. Under-declaring an astral character sends only its
            # high surrogate, so the count is deliberate, never len(char).
            units = utf16_length(char)
            down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
            Quartz.CGEventKeyboardSetUnicodeString(down, units, char)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
            up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
            Quartz.CGEventKeyboardSetUnicodeString(up, units, char)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

    def parse_key_combo(self, combo: str) -> tuple[int, int]:
        return parse_key_combo(combo)

    def press_keys(self, combo: str) -> None:
        if not _is_host_identity():
            _require_ok(_cu_transact(f"key {combo}"), "key")
            return
        _require_accessibility("key")
        import Quartz

        flags, keycode = parse_key_combo(combo)
        for pressed in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, keycode, pressed)
            if flags:
                Quartz.CGEventSetFlags(event, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def scroll(self, dx: int, dy: int) -> None:
        if not _is_host_identity():
            _require_ok(_cu_transact(f"scroll {dx} {dy}"), "scroll")
            return
        _require_accessibility("scroll")
        import Quartz

        # wheelCount=2: wheel1 is vertical (dy), wheel2 is horizontal (dx).
        event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, dy, dx)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    # --- state read-back ----------------------------------------------------

    def cursor_position(self) -> tuple[int, int]:
        """Pointer position in global points, top-left origin.

        ``CGEventGetLocation`` on a null event reports the current location
        already in the top-left space the rest of this backend uses. The
        alternative, ``NSEvent.mouseLocation``, is bottom-left and would need
        flipping against the main display height.
        """
        import Quartz

        point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return (round(point.x), round(point.y))

    def foreground_window(self) -> WindowInfo:
        """The frontmost application's window, best-effort.

        macOS splits this: the frontmost *application* is public API, but window
        *titles* live behind Accessibility. So the process name is always
        available and the title is filled in from the on-screen window list when
        macOS is willing, empty when it is not — which
        :func:`~tst_cu_mcp.focus.window_matches` handles, since it matches either
        field.
        """
        import Quartz
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:  # pragma: no cover - no active app is possible at login
            return WindowInfo(title="", process="", pid=0, x=0, y=0, width=0, height=0)

        pid = int(app.processIdentifier())
        process = str(app.localizedName() or "")

        title = ""
        x = y = width = height = 0
        listing = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        for entry in listing or []:
            if int(entry.get("kCGWindowOwnerPID", -1)) != pid:
                continue
            # The list is front-to-back, so the owner's first entry is its
            # frontmost window.
            title = str(entry.get("kCGWindowName") or "")
            bounds = entry.get("kCGWindowBounds") or {}
            x = round(float(bounds.get("X", 0)))
            y = round(float(bounds.get("Y", 0)))
            width = round(float(bounds.get("Width", 0)))
            height = round(float(bounds.get("Height", 0)))
            break

        return WindowInfo(
            title=title,
            process=process,
            pid=pid,
            x=x,
            y=y,
            width=width,
            height=height,
        )

    # --- environment --------------------------------------------------------

    def check_permissions(self, *, request: bool = False) -> dict[str, Any]:
        """Probe Screen Recording and Accessibility, optionally prompting first.

        Outside the host identity this is a socket client: the TST Desk host
        answers with its own diagnosis (``stale_grant_suspected``, ``fix``,
        ``identity``), returned verbatim (TD-4823). ``request=true`` raises
        each OS prompt at most once (stamp + process memory) and only from
        the host identity — a checkout interpreter's CGRequest is attributed
        to TST Desk but never attaches to this process, so it loops forever.
        """
        if not _is_host_identity():
            host_report = _permissions_via_agent(request=request)
            if host_report is not None:
                return host_report

        screen_recording = _screen_recording_granted()
        accessibility = _accessibility_granted()

        if request and _is_host_identity():
            if not screen_recording and not _already_prompted(SCREEN_RECORDING):
                _request_screen_recording()
                _mark_prompted(SCREEN_RECORDING)
                screen_recording = _screen_recording_granted()
            if not accessibility and not _already_prompted(ACCESSIBILITY):
                _request_accessibility()
                _mark_prompted(ACCESSIBILITY)
                accessibility = _accessibility_granted()

        report = build_report(screen_recording=screen_recording, accessibility=accessibility)
        if not _is_host_identity():
            # No host socket: nothing here can capture or click as TST Desk.
            # The prompt stamps stay untouched — they are meaningless for a
            # socket client and would block a future legitimate prompt.
            report["actuation_path"] = "none"
            report["fix"] = {**report.get("fix", {}), "host": NO_HOST_FIX}
        return report


def _already_prompted(kind: str) -> bool:
    if kind in _prompted_this_process:
        return True
    try:
        return (PROMPT_STAMP_DIR / kind).is_file()
    except OSError:
        return False


def _mark_prompted(kind: str) -> None:
    _prompted_this_process.add(kind)
    try:
        PROMPT_STAMP_DIR.mkdir(parents=True, exist_ok=True)
        (PROMPT_STAMP_DIR / kind).write_text("1\n", encoding="utf-8")
    except OSError:
        return


def _screen_recording_granted() -> bool:
    """True if capture is allowed for this process *or* its host app.

    ``CGPreflightScreenCaptureAccess`` is this interpreter's own TCC row.
    After the user grants TST Desk (or Terminal, Claude Desktop, ...), that
    preflight stays false here, but window titles become readable because
    macOS attributes Screen Recording to the responsible host. Treat either
    as granted so we stop re-prompting.
    """
    return _cg_preflight() or _window_titles_visible()


def _cg_preflight() -> bool:
    """True if this process itself holds Screen Recording. Fails closed."""
    try:
        from Quartz import CGPreflightScreenCaptureAccess
    except (ImportError, AttributeError):
        return False
    return bool(CGPreflightScreenCaptureAccess())


def _is_host_identity() -> bool:
    """True when this process is the app sidecar, not a checkout helper."""
    if os.environ.get("TST_CU_MCP_HOST", "").strip() == "1":
        return True
    if getattr(sys, "frozen", False):
        return Path(sys.executable).name.startswith("tstd")
    return False


def _host_tstd() -> Path | None:
    """The TST Desk sidecar binary, which holds the host TCC identity."""
    names = ("TST Desk.app/Contents/MacOS/tstd",)
    candidates: list[Path] = []
    env = os.environ.get("TST_CU_HOST", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable))
    candidates.append(Path("/Applications") / names[0])
    candidates.append(Path.home() / "Applications" / names[0])
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    return None


def _sock_path() -> Path | None:
    env = os.environ.get(SOCK_ENV, "").strip()
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env))
    candidates.append(
        Path.home()
        / "Library"
        / "Application Support"
        / "com.thatsimpletech.tstdesk"
        / "cu-agent.sock"
    )
    for path in candidates:
        try:
            if path.exists():
                return path
        except OSError:
            continue
    return None


def _cu_transact(line: str, *, binary: bool = False, timeout: float | None = None) -> bytes:
    path = _sock_path()
    if path is None:
        return b""
    wait = _CAPTURE_TIMEOUT if binary else _TEXT_TIMEOUT
    if timeout is not None:
        wait = timeout
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(wait)
    try:
        sock.connect(str(path))
        sock.sendall(line.encode("utf-8") + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        if b"\n" not in buf:
            return buf
        header, rest = buf.split(b"\n", 1)
        if binary and header.startswith(b"png "):
            n = int(header.split()[1])
            data = rest
            while len(data) < n:
                chunk = sock.recv(n - len(data))
                if not chunk:
                    break
                data += chunk
            return data[:n]
        return header + b"\n"
    except (OSError, ValueError, TimeoutError):
        return b""
    finally:
        sock.close()


def _refusal(op: str) -> RuntimeError:
    return RuntimeError(
        f"computer-use {op} failed in the TST Desk host. Enable TST Desk in "
        "System Settings > Privacy & Security > Accessibility, then Quit (Cmd+Q) "
        "and reopen it. If System Settings already shows TST Desk ON, the grant "
        "belongs to an older build of the app: ask the user to click Reset grants "
        "in TST Desk → Settings → Computer use (or run `tccutil reset "
        "Accessibility com.thatsimpletech.tstdesk`), relaunch, and allow again. "
        "Do not retry this action until check_permissions reports it granted."
    )


def _require_ok(raw: bytes, op: str) -> None:
    if raw.strip() != b"ok":
        raise _refusal(op)


def _require_accessibility(op: str) -> None:
    """Refuse before posting: macOS drops CGEventPost from a process without
    Accessibility and says nothing, so the model would hear "ok" for a click
    that never landed (the host socket path already refuses this way)."""
    if not _accessibility_granted():
        raise _refusal(op)


def _capture_via_agent(rect: Rect) -> bytes | None:
    x, y, w, h = rect
    raw = _cu_transact(f"capture {x} {y} {w} {h}", binary=True)
    if raw.startswith(b"\x89PNG"):
        return raw
    return None


def _permissions_via_agent(*, request: bool = False) -> dict[str, Any] | None:
    raw = _cu_transact("permissions request" if request else "permissions")
    try:
        parsed: Any = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict) and "screen_recording" in parsed:
        return parsed
    return None


def _cg_capture_png(rect: Rect) -> bytes | None:
    """Grab ``rect`` via CoreGraphics. Returns None on failure. Never prompts."""
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        return None
    try:
        from Quartz import (
            CGImageGetHeight,
            CGImageGetWidth,
            CGRectMake,
            CGWindowListCreateImage,
            kCGNullWindowID,
            kCGWindowImageDefault,
            kCGWindowListOptionOnScreenOnly,
        )
    except (ImportError, AttributeError):
        return None
    try:
        image = CGWindowListCreateImage(
            CGRectMake(float(x), float(y), float(w), float(h)),
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
            kCGWindowImageDefault,
        )
    except Exception:
        return None
    if image is None:
        return None
    try:
        if int(CGImageGetWidth(image)) < 1 or int(CGImageGetHeight(image)) < 1:
            return None
    except Exception:
        return None
    return _png_from_cgimage(image)


def _png_from_cgimage(image: Any) -> bytes | None:
    """Encode a ``CGImage`` as PNG bytes. Fails closed."""
    try:
        from AppKit import NSBitmapImageFileTypePNG, NSBitmapImageRep
    except (ImportError, AttributeError):
        return None
    try:
        rep = NSBitmapImageRep.alloc().initWithCGImage_(image)
        if rep is None:
            return None
        data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
    except Exception:
        return None
    if data is None:
        return None
    raw = bytes(data)
    if not raw.startswith(b"\x89PNG"):
        return None
    return raw


def _window_titles_visible() -> bool:
    """True when on-screen window titles are not blanked by TCC.

    macOS strips ``kCGWindowName`` without Screen Recording. A child of a
    granted host can often read titles even when CGPreflight is false.
    """
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListExcludeDesktopElements,
            kCGWindowListOptionOnScreenOnly,
        )
    except (ImportError, AttributeError):
        return False
    try:
        listing = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )
    except Exception:
        return False
    for entry in listing or []:
        getter = getattr(entry, "get", None)
        if getter is None:
            continue
        try:
            name = getter("kCGWindowName")
        except Exception:
            continue
        if isinstance(name, str) and name.strip():
            return True
    return False


def _accessibility_granted() -> bool:
    """True if this process, or a child of a trusted host, can use AX APIs."""
    return _ax_process_trusted() or _ax_api_usable()


def _ax_process_trusted() -> bool:
    """True if this process is a trusted Accessibility client."""
    try:
        from ApplicationServices import AXIsProcessTrusted
    except (ImportError, AttributeError):
        return False
    return bool(AXIsProcessTrusted())


def _ax_api_usable() -> bool:
    """True when a harmless AX read succeeds.

    Granting the host app does not always flip ``AXIsProcessTrusted`` in a
    spawned interpreter. If we can still read the frontmost app's role, input
    synthesis is going to work too.
    """
    try:
        from AppKit import NSWorkspace
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
        )
    except (ImportError, AttributeError):
        return False
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return False
        element = AXUIElementCreateApplication(int(app.processIdentifier()))
        result = AXUIElementCopyAttributeValue(element, "AXRole", None)
    except Exception:
        return False
    if result is None:
        return False
    if isinstance(result, tuple):
        err = result[0]
        return err in (0, None)
    return True


def _request_screen_recording() -> bool:
    """Trigger the Screen Recording prompt (first time only). Returns current grant."""
    try:
        from Quartz import CGRequestScreenCaptureAccess
    except (ImportError, AttributeError):
        return False
    return bool(CGRequestScreenCaptureAccess())


def _request_accessibility() -> bool:
    """Open the Accessibility prompt if not yet trusted. Returns current trust."""
    try:
        from ApplicationServices import (
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
    except (ImportError, AttributeError):
        return False
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))
