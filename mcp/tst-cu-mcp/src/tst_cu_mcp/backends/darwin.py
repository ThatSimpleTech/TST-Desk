"""macOS backend: CoreGraphics for geometry and input, ``screencapture`` for pixels.

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

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from tst_cu_mcp.backends.base import Rect
from tst_cu_mcp.displays import DisplayInfo
from tst_cu_mcp.focus import WindowInfo
from tst_cu_mcp.permissions import build_report

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
        """Capture via ``screencapture -x`` into a temp file that is unlinked.

        The file is removed in a ``finally``, so a screenshot never outlives the
        call even if reading it raises.
        """
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
        import Quartz

        event = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def click(self, x: float, y: float, button: str, count: int) -> None:
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
        import Quartz

        for char in text:
            down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
            Quartz.CGEventKeyboardSetUnicodeString(down, len(char), char)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
            up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
            Quartz.CGEventKeyboardSetUnicodeString(up, len(char), char)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

    def parse_key_combo(self, combo: str) -> tuple[int, int]:
        return parse_key_combo(combo)

    def press_keys(self, combo: str) -> None:
        import Quartz

        flags, keycode = parse_key_combo(combo)
        for pressed in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, keycode, pressed)
            if flags:
                Quartz.CGEventSetFlags(event, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def scroll(self, dx: int, dy: int) -> None:
        import Quartz

        # wheelCount=2: wheel1 is vertical (dy), wheel2 is horizontal (dx).
        event = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 2, dy, dx
        )
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
        """Probe Screen Recording and Accessibility, optionally prompting first."""
        screen_recording = _screen_recording_granted()
        accessibility = _accessibility_granted()

        if request:
            if not screen_recording:
                _request_screen_recording()
                screen_recording = _screen_recording_granted()
            if not accessibility:
                _request_accessibility()
                accessibility = _accessibility_granted()

        return build_report(screen_recording=screen_recording, accessibility=accessibility)


def _screen_recording_granted() -> bool:
    """True if the host process holds Screen Recording permission.

    Fails closed (returns ``False``) if the API is unavailable.
    """
    try:
        from Quartz import CGPreflightScreenCaptureAccess
    except (ImportError, AttributeError):
        return False
    return bool(CGPreflightScreenCaptureAccess())


def _accessibility_granted() -> bool:
    """True if the host process is a trusted Accessibility client."""
    try:
        from ApplicationServices import AXIsProcessTrusted
    except (ImportError, AttributeError):
        return False
    return bool(AXIsProcessTrusted())


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
