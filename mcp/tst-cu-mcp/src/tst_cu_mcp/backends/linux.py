"""Linux backend: X11 via ctypes for geometry and input, Pillow for pixels.

Coordinates are global **physical pixels** with a top-left origin, spanning the
RandR virtual desktop (a monitor left of the primary has a negative origin,
exactly as on Windows). Pillow already knows how to grab that space; XTest
moves the pointer in the same coordinates.

A Wayland session is detected from the environment only — health must answer
without opening a Display. The backend still constructs without OS calls.
"""

from __future__ import annotations

import os
from typing import Any

from tst_cu_mcp.backends.base import Rect
from tst_cu_mcp.backends.linux_keys import (
    KEYCODES,
    MODS,
    UNSUPPORTED_MODS,
    XK_SHIFT_L,
    char_to_keysym,
    parse_key_combo,
)
from tst_cu_mcp.displays import DisplayInfo
from tst_cu_mcp.focus import WindowInfo
from tst_cu_mcp.permissions import build_linux_report

__all__ = [
    "BUTTONS",
    "KEYCODES",
    "MODS",
    "UNSUPPORTED_MODS",
    "LinuxBackend",
    "WaylandUnsupportedError",
    "linux_session_kind",
    "linux_session_usable",
    "order_displays",
    "parse_key_combo",
    "require_native_x11",
]

BUTTONS = {"left": 1, "right": 3}


def linux_session_kind() -> str:
    """Return ``x11``, ``wayland``, or ``unknown`` from the environment.

    No X connection: ``health`` must stay callable on a headless host and must
    not treat an XWayland ``DISPLAY`` on a Wayland session as support.
    """
    session = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if session == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        if session == "x11":
            return "x11"
        return "wayland"
    if session == "x11" or os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def linux_session_usable() -> bool:
    """True when this process is in a native X11 session."""
    return linux_session_kind() == "x11"


class WaylandUnsupportedError(RuntimeError):
    """Native Wayland cannot be captured or driven (TD-2002).

    Distinct from a missing backend: ``get_backend()`` still returns
    ``LinuxBackend`` so ``health`` / ``check_permissions`` can name
    ``session_type``. Capture and input must not follow that selection
    onto an XWayland ``DISPLAY``.
    """


def require_native_x11() -> None:
    """Refuse before any Xlib call when this is not a native X11 session.

    An XWayland ``DISPLAY`` on a Wayland session does not count as support:
    it would only drive X11 clients and would lie about native apps (TD-2001).
    """
    if linux_session_usable():
        return
    kind = linux_session_kind()
    raise WaylandUnsupportedError(
        "Linux computer-use is X11 only (TD-2002); "
        f"session_type={kind!r} is not supported. "
        "An XWayland DISPLAY is not a substitute for native Wayland apps."
    )


def order_displays(
    monitors: list[tuple[int, int, int, int, int, bool, float]],
) -> list[DisplayInfo]:
    """Index RandR outputs: primary first, then top-to-bottom, left-to-right."""
    ordered = sorted(monitors, key=lambda m: (not m[5], m[2], m[1]))
    return [
        DisplayInfo(
            display_id=handle,
            index=index,
            x=left,
            y=top,
            width=width,
            height=height,
            scale=scale,
            is_main=is_primary,
        )
        for index, (handle, left, top, width, height, is_primary, scale) in enumerate(ordered)
    ]


class LinuxBackend:
    """X11 eyes and hands."""

    name = "linux"

    def list_displays(self) -> list[DisplayInfo]:
        require_native_x11()
        from tst_cu_mcp.backends import linux_x11

        return order_displays(linux_x11.list_raw_displays())

    def capture_png(self, rect: Rect) -> bytes:
        require_native_x11()
        from tst_cu_mcp.backends import linux_x11

        return linux_x11.capture_png(rect)

    def move_mouse(self, x: float, y: float) -> None:
        require_native_x11()
        from tst_cu_mcp.backends import linux_x11

        linux_x11.move_mouse(x, y)

    def click(self, x: float, y: float, button: str, count: int) -> None:
        require_native_x11()
        from tst_cu_mcp.backends import linux_x11

        self.move_mouse(x, y)
        linux_x11.click_button(BUTTONS[button], count)

    def type_text(self, text: str) -> None:
        require_native_x11()
        from tst_cu_mcp.backends import linux_x11

        for ch in text:
            keysym, needs_shift = char_to_keysym(ch)
            if needs_shift:
                linux_x11.press_keysym(XK_SHIFT_L, down=True)
            linux_x11.press_keysym(keysym, down=True)
            linux_x11.press_keysym(keysym, down=False)
            if needs_shift:
                linux_x11.press_keysym(XK_SHIFT_L, down=False)

    def parse_key_combo(self, combo: str) -> tuple[tuple[int, ...], int]:
        return parse_key_combo(combo)

    def press_keys(self, combo: str) -> None:
        require_native_x11()
        from tst_cu_mcp.backends import linux_x11

        mod_syms, base = parse_key_combo(combo)
        for sym in mod_syms:
            linux_x11.press_keysym(sym, down=True)
        linux_x11.press_keysym(base, down=True)
        linux_x11.press_keysym(base, down=False)
        for sym in reversed(mod_syms):
            linux_x11.press_keysym(sym, down=False)

    def scroll(self, dx: int, dy: int) -> None:
        require_native_x11()
        from tst_cu_mcp.backends import linux_x11

        linux_x11.scroll_buttons(dx, dy)

    def cursor_position(self) -> tuple[int, int]:
        require_native_x11()
        from tst_cu_mcp.backends import linux_x11

        return linux_x11.cursor_position()

    def foreground_window(self) -> WindowInfo:
        require_native_x11()
        from tst_cu_mcp.backends import linux_x11

        return linux_x11.foreground_window()

    def check_permissions(self, *, request: bool = False) -> dict[str, Any]:
        """X11 has no grant dialog. ``request`` is accepted and ignored."""
        del request
        from tst_cu_mcp.backends import linux_x11

        session = linux_session_kind()
        display_ok = False
        xtest_ok = False
        if session == "x11":
            display_ok, xtest_ok = linux_x11.probe_display()
        return build_linux_report(
            session=session,
            display=display_ok,
            xtest=xtest_ok,
        )

    def hit_test(self, x: float, y: float) -> dict[str, Any]:
        """AT-SPI node, or the EWMH window under the point. Never actuates."""
        require_native_x11()
        from tst_cu_mcp.backends.linux_hit import observe_at

        return observe_at(x, y)
