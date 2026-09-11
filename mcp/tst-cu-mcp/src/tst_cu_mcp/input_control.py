"""Synthesized mouse and keyboard input.

This module is the policy layer and holds no platform code. Every public action
does the same four things in the same order before the OS is touched at all:

1. ask the kill-switch (:func:`tst_cu_mcp.safety.ensure_actuation_allowed`),
2. validate arguments and bounds,
3. check the foreground window against ``expect_window``, if given,
4. delegate to the active backend.

Keeping that order here, rather than in each backend, is deliberate: a new
platform cannot ship without the kill-switch, the off-screen check or the focus
guard, because it never gets the chance to forget them.

Immediately after the kill-switch passes, every action pings
:func:`tst_cu_mcp.overlay.get_overlay` so the real-display glow tracks the
attempt — including attempts that then fail validation, which are still the
agent driving. A refusal from the backend itself — the OS said no, as with a
missing Accessibility grant — puts the glow out again
(:func:`tst_cu_mcp.safety.notify_blocked`): nothing was driven.

The order within it matters too. The kill-switch comes first because "stop"
should not depend on anything else being well-formed. Argument validation comes
before the focus check so a malformed call fails on its own merits rather than
on whatever happened to be in front. And the focus check is last before
actuation, so it reflects the state at the moment of acting rather than the
moment of parsing.

All coordinates are global coordinates in the backend's space (the mapper's
output), top-left origin.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tst_cu_mcp import safety
from tst_cu_mcp.backends import get_backend
from tst_cu_mcp.displays import list_displays
from tst_cu_mcp.focus import assert_foreground
from tst_cu_mcp.overlay import get_overlay

MOUSE_BUTTONS = ("left", "right")
# Counted in UTF-16 code units, not Python characters — see utf16_length.
MAX_TEXT_LEN = 10000
MAX_SCROLL_LINES = 10000
# Real multi-click semantics end around triple-click; this bounds the worst
# case (a model looping clicks at one point) far below harmful while leaving
# every legitimate spelling room.
MAX_CLICK_COUNT = 100


def _actuate(act: Callable[..., None], *args: Any) -> None:
    """Run one backend act; if the OS refuses it, the glow goes dark."""
    try:
        act(*args)
    except Exception:
        safety.notify_blocked()
        raise


def _display_bounds_union() -> tuple[int, int, int, int] | None:
    """Return (min_x, min_y, max_x, max_y) across all displays, or None if none."""
    displays = list_displays()
    if not displays:
        return None
    min_x = min(d.x for d in displays)
    min_y = min(d.y for d in displays)
    max_x = max(d.x + d.width for d in displays)
    max_y = max(d.y + d.height for d in displays)
    return (min_x, min_y, max_x, max_y)


def assert_on_screen(x: float, y: float) -> None:
    """Raise ValueError if (x, y) is outside the union of all displays."""
    bounds = _display_bounds_union()
    if bounds is None:
        # Wayland with no ScreenCast grabber returns an empty list, not
        # X11's "no displays". Name the portal so this is not confused
        # with a headless Xvfb box (TD-2001 / TD-4901).
        if get_backend().name == "wayland":
            from tst_cu_mcp.backends.wayland_input import WaylandInputError

            raise WaylandInputError(
                "Wayland input requires portal RemoteDesktop / libei "
                "(TD-4901b). An XWayland DISPLAY is not a substitute."
            )
        raise RuntimeError("no active displays found")
    min_x, min_y, max_x, max_y = bounds
    if not (min_x <= x <= max_x and min_y <= y <= max_y):
        raise ValueError(
            f"point ({x:.0f},{y:.0f}) is off-screen; "
            f"screen bounds x[{min_x},{max_x}] y[{min_y},{max_y}]"
        )


def cursor_position() -> tuple[int, int]:
    """Where the pointer is now, in global coordinates.

    A read, not an action: no kill-switch check, because halting the hands should
    not blind the eyes. A caller that has just been refused still needs to be
    able to see where things stand.
    """
    return get_backend().cursor_position()


def move_mouse(x: float, y: float, expect_window: str | None = None) -> None:
    """Move the cursor to a global coordinate."""
    safety.ensure_actuation_allowed()
    get_overlay().activity()
    assert_on_screen(x, y)
    assert_foreground(expect_window)
    _actuate(get_backend().move_mouse, float(x), float(y))


def click(
    x: float,
    y: float,
    button: str = "left",
    count: int = 1,
    expect_window: str | None = None,
) -> None:
    """Click at a global coordinate. ``count`` >= 2 produces a multi-click."""
    safety.ensure_actuation_allowed()
    get_overlay().activity()
    if button not in MOUSE_BUTTONS:
        raise ValueError(f"unknown button {button!r}; use one of {MOUSE_BUTTONS}")
    if count < 1:
        raise ValueError("count must be >= 1")
    if count > MAX_CLICK_COUNT:
        raise ValueError(f"click count too large ({count} > {MAX_CLICK_COUNT})")
    assert_on_screen(x, y)
    assert_foreground(expect_window)
    _actuate(get_backend().click, float(x), float(y), button, count)


def parse_key_combo(combo: str) -> object:
    """Parse a key combo into the active platform's pressable form.

    The parsed shape is platform-specific — macOS returns
    ``(modifier_flags, keycode)``, Windows returns ``(modifier_vks, base_vk)`` —
    because the two operating systems express modifiers differently. Callers
    that only need to know whether a combo is *valid* can ignore the value.
    """
    return get_backend().parse_key_combo(combo)


def utf16_length(text: str) -> int:
    """Length of *text* measured in UTF-16 code units rather than characters.

    Python's ``len()`` counts code points, and every character above U+FFFF
    (astral plane: emoji, CJK Extension B+) is one of those but two UTF-16
    units — two keyboard events on Windows. Validating against ``len(text)``
    would let the real event volume reach twice the stated cap, so the bound is
    counted in the unit the backends actually emit.
    """
    return len(text.encode("utf-16-le")) // 2


def type_text(text: str, expect_window: str | None = None) -> None:
    """Type a Unicode string at the current focus. The text is never logged."""
    safety.ensure_actuation_allowed()
    get_overlay().activity()
    units = utf16_length(text)
    if units > MAX_TEXT_LEN:
        raise ValueError(f"text too long ({units} UTF-16 units > {MAX_TEXT_LEN})")
    assert_foreground(expect_window)
    if text:
        _actuate(get_backend().type_text, text)


def press_keys(combo: str, expect_window: str | None = None) -> None:
    """Press and release a key combo such as ``"ctrl+c"`` or ``"return"``."""
    safety.ensure_actuation_allowed()
    get_overlay().activity()
    backend = get_backend()
    # Parse before actuating so an unknown key is a clean error rather than a
    # half-pressed modifier left down on the user's keyboard.
    backend.parse_key_combo(combo)
    assert_foreground(expect_window)
    _actuate(backend.press_keys, combo)


def scroll(dx: int, dy: int, expect_window: str | None = None) -> None:
    """Scroll by lines: dy>0 up, dy<0 down; dx>0 right, dx<0 left."""
    safety.ensure_actuation_allowed()
    get_overlay().activity()
    if abs(dx) > MAX_SCROLL_LINES or abs(dy) > MAX_SCROLL_LINES:
        raise ValueError(f"scroll magnitude too large (max {MAX_SCROLL_LINES} lines)")
    assert_foreground(expect_window)
    if dx or dy:
        _actuate(get_backend().scroll, int(dx), int(dy))
