"""Agent cursors for the macOS helper (TD-5227).

Each agent gets a small borderless panel: an arrow in its color with a
white rim, and a pill with its label. Like the ring's panels these are
click-through, never take focus, join every space, and are left out of
every screen capture (``NSWindowSharingNone``), so no agent ever sees a
cursor in a screenshot. They are windows, not a cursor API: the hardware
pointer is never moved or replaced.

The motion lives in :class:`CursorModel`, plain Python the tests drive with
a fake clock: a cursor glides to each new point, a click plays a short
ring, and an agent idle for :data:`CURSOR_IDLE_SECONDS` fades out. With
Reduce Motion on, a cursor jumps and nothing ripples. Only
:func:`build_cursor_layer` touches AppKit.

Line protocol (handled by the helper's main thread):

* ``cursor <key> <x> <y> <#rrggbb> <click 0|1> <label...>`` — global
  point in points, top-left origin (the CGEvent space).
* ``cursor_hide <key>`` or ``cursor_hide *`` — hide now, no fade.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: An agent with no new action for this long fades out.
CURSOR_IDLE_SECONDS = 10.0
#: Glide speed: the share of the remaining distance covered per second.
GLIDE_RATE = 14.0
FADE_IN_RATE = 8.0
FADE_OUT_RATE = 3.0
RIPPLE_SECONDS = 0.45

#: Panel size and where the arrow's tip sits in it (points, top-left).
PANEL_WIDTH = 300.0
PANEL_HEIGHT = 72.0
TIP_X = 16.0
TIP_Y = 16.0
#: Above the ring (1000), still below the system cursor.
CURSOR_WINDOW_LEVEL = 1001

#: The arrow, as offsets from its tip with y growing downward.
ARROW = (
    (0.0, 0.0),
    (0.0, 19.0),
    (4.5, 15.0),
    (7.6, 22.0),
    (10.6, 20.7),
    (7.6, 13.9),
    (13.4, 13.9),
)


@dataclass
class CursorState:
    """One agent's cursor: where it is drawn, where it is going, how lit."""

    key: str
    color: str
    label: str
    x: float
    y: float
    tx: float
    ty: float
    last_seen: float
    alpha: float = 0.0
    ripple: float = 0.0
    shown: bool = True


@dataclass
class CursorModel:
    """Every agent's cursor, advanced once per helper tick."""

    cursors: dict[str, CursorState] = field(default_factory=dict)

    def place(
        self, key: str, x: float, y: float, color: str, label: str, click: bool, now: float
    ) -> None:
        state = self.cursors.get(key)
        if state is None or state.alpha <= 0.01 or not state.shown:
            # A cursor coming back from hidden starts where it is going,
            # not at a stale spot from minutes ago.
            state = CursorState(key, color, label, x, y, x, y, now)
            self.cursors[key] = state
        state.tx, state.ty = x, y
        state.color, state.label = color, label
        state.last_seen = now
        state.shown = True
        if click:
            state.ripple = 1.0

    def hide(self, key: str | None) -> None:
        for state in self.cursors.values():
            if key is None or state.key == key:
                state.shown = False
                state.alpha = 0.0
                state.ripple = 0.0

    def step(self, now: float, dt: float, reduce_motion: bool) -> None:
        for state in self.cursors.values():
            if reduce_motion:
                state.x, state.y = state.tx, state.ty
                state.ripple = 0.0
            else:
                share = min(1.0, max(0.0, dt) * GLIDE_RATE)
                state.x += (state.tx - state.x) * share
                state.y += (state.ty - state.y) * share
                state.ripple = max(0.0, state.ripple - dt / RIPPLE_SECONDS)
            live = state.shown and now - state.last_seen < CURSOR_IDLE_SECONDS
            goal = 1.0 if live else 0.0
            rate = FADE_IN_RATE if goal > state.alpha else FADE_OUT_RATE
            state.alpha += (goal - state.alpha) * min(1.0, max(0.0, dt) * rate)
            if goal == 0.0 and state.alpha < 0.02:
                state.alpha = 0.0


def parse_cursor_args(arg: str) -> tuple[str, float, float, str, bool, str] | None:
    """``cursor``'s argument, or ``None`` when it is malformed."""
    parts = arg.split(" ", 5)
    if len(parts) < 5:
        return None
    key, raw_x, raw_y, color, raw_click = parts[:5]
    label = parts[5].strip() if len(parts) == 6 else ""
    try:
        x, y = float(raw_x), float(raw_y)
    except ValueError:
        return None
    if not (math.isfinite(x) and math.isfinite(y)) or parse_hex(color) is None or not key:
        return None
    return key, x, y, color, raw_click == "1", label or "Agent"


def parse_hex(color: str) -> tuple[float, float, float] | None:
    """``#rrggbb`` as 0..1 floats."""
    if len(color) != 7 or not color.startswith("#"):
        return None
    try:
        return tuple(int(color[i : i + 2], 16) / 255.0 for i in (1, 3, 5))  # type: ignore[return-value]
    except ValueError:
        return None


def panel_origin(x: float, y: float, primary_height: float) -> tuple[float, float]:
    """Cocoa origin (bottom-left, y up) for a panel whose tip is at (x, y)."""
    return x - TIP_X, primary_height - y + TIP_Y - PANEL_HEIGHT


def build_cursor_layer() -> Any:
    """Define the AppKit view and return a layer the helper ticks."""
    from AppKit import NSView

    class CursorView(NSView):
        """Draws one agent's arrow, click ring and label pill."""

        def isFlipped(self) -> bool:
            return True

        def drawRect_(self, _rect: Any) -> None:
            from AppKit import NSBezierPath, NSColor, NSFont, NSString
            from Foundation import NSMakePoint, NSMakeRect

            red, green, blue = getattr(self, "fill", (0.18, 0.43, 0.87))
            fill = NSColor.colorWithCalibratedRed_green_blue_alpha_(red, green, blue, 1.0)
            white = NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.95)
            ripple = float(getattr(self, "ripple", 0.0))
            if ripple > 0.0:
                radius = 4.0 + 14.0 * (1.0 - ripple)
                ring = NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(TIP_X - radius, TIP_Y - radius, 2 * radius, 2 * radius)
                )
                ring.setLineWidth_(2.0)
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    red, green, blue, 0.7 * ripple
                ).setStroke()
                ring.stroke()
            arrow = NSBezierPath.bezierPath()
            first, *rest = ARROW
            arrow.moveToPoint_(NSMakePoint(TIP_X + first[0], TIP_Y + first[1]))
            for dx, dy in rest:
                arrow.lineToPoint_(NSMakePoint(TIP_X + dx, TIP_Y + dy))
            arrow.closePath()
            arrow.setLineJoinStyle_(1)
            fill.setFill()
            arrow.fill()
            arrow.setLineWidth_(1.5)
            white.setStroke()
            arrow.stroke()
            text = NSString.stringWithString_(str(getattr(self, "text", "")))
            attrs = {
                "NSFont": NSFont.systemFontOfSize_weight_(11.0, 0.3),
                "NSColor": NSColor.whiteColor(),
            }
            size = text.sizeWithAttributes_(attrs)
            left, top = TIP_X + 14.0, TIP_Y + 18.0
            width = min(size.width + 14.0, PANEL_WIDTH - left - 2.0)
            height = size.height + 6.0
            pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(left, top, width, height), height / 2.0, height / 2.0
            )
            fill.setFill()
            pill.fill()
            pill.setLineWidth_(1.0)
            white.setStroke()
            pill.stroke()
            text.drawInRect_withAttributes_(
                NSMakeRect(left + 7.0, top + 3.0, width - 14.0, size.height), attrs
            )

    return CursorLayer(CursorView)


class CursorLayer:
    """The helper's cursors: the model plus one panel per agent."""

    def __init__(self, view_class: Any) -> None:
        self.model = CursorModel()
        self.panels: dict[str, Any] = {}
        self._view_class = view_class
        self._drawn: dict[str, tuple[str, str, float]] = {}
        self._primary_height = 0.0
        self.screens_changed()

    def screens_changed(self) -> None:
        from AppKit import NSScreen

        screens = NSScreen.screens()
        self._primary_height = float(screens[0].frame().size.height) if screens else 0.0

    def command(self, name: str, arg: str, now: float) -> None:
        if name == "cursor_hide":
            key = arg.strip()
            self.model.hide(None if key in ("", "*") else key)
            self._apply_hidden()
            return
        parsed = parse_cursor_args(arg)
        if parsed is not None:
            key, x, y, color, click, label = parsed
            self.model.place(key, x, y, color, label, click, now)

    def hide_now(self) -> None:
        """Out of the way for a capture; the next tick puts them back."""
        for panel in self.panels.values():
            panel.orderOut_(None)

    def tick(self, now: float, dt: float, reduce_motion: bool, grabbing: bool) -> None:
        self.model.step(now, dt, reduce_motion)
        for key, state in self.model.cursors.items():
            if state.alpha <= 0.0 or grabbing:
                if key in self.panels:
                    self.panels[key].orderOut_(None)
                continue
            panel = self.panels.get(key) or self._make_panel(key)
            self._redraw(key, state)
            panel.setFrameOrigin_(panel_origin(state.x, state.y, self._primary_height))
            panel.setAlphaValue_(state.alpha)
            panel.orderFrontRegardless()

    def _apply_hidden(self) -> None:
        for key, state in self.model.cursors.items():
            if state.alpha <= 0.0 and key in self.panels:
                self.panels[key].orderOut_(None)

    def _redraw(self, key: str, state: CursorState) -> None:
        look = (state.color, state.label, round(state.ripple, 2))
        if self._drawn.get(key) == look:
            return
        self._drawn[key] = look
        view = self.panels[key].contentView()
        view.fill = parse_hex(state.color) or (0.18, 0.43, 0.87)
        view.text = state.label
        view.ripple = state.ripple
        view.setNeedsDisplay_(True)

    def _make_panel(self, key: str) -> Any:
        from AppKit import (
            NSColor,
            NSPanel,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSWindowSharingNone,
            NSWindowStyleMaskBorderless,
            NSWindowStyleMaskNonactivatingPanel,
        )
        from Foundation import NSMakeRect

        frame = NSMakeRect(0.0, 0.0, PANEL_WIDTH, PANEL_HEIGHT)
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            2,
            False,
        )
        panel.setLevel_(CURSOR_WINDOW_LEVEL)
        panel.setIgnoresMouseEvents_(True)
        panel.setSharingType_(NSWindowSharingNone)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        panel.setContentView_(self._view_class.alloc().initWithFrame_(frame))
        panel.setAlphaValue_(0.0)
        self.panels[key] = panel
        return panel


__all__ = [
    "CURSOR_IDLE_SECONDS",
    "CursorModel",
    "CursorState",
    "build_cursor_layer",
    "panel_origin",
    "parse_cursor_args",
    "parse_hex",
]
