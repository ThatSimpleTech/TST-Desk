"""The glow painter: a tiny AppKit process owned by the sidecar.

One borderless, click-through, topmost panel per display strokes a rust
ring around the screen edges while computer use is active — same accent
as tst-desk's Screen-pane glow, so "the agent is driving" reads the same
in both places. The ring pulses like ``GlowLayer``'s CSS animation, sits
at the screen-saver window level so it floats above everything, ignores
mouse events so it can never eat input, and stays up until an explicit hide
(computer-use session close), not a linger timeout.

Why a separate process: AppKit wants a runloop and the MCP server's loop
is asyncio over stdio. A child that owns ``NSApplication.run()`` keeps
both simple, and crashing here cannot take computer use down with it.

Protocol (one command per stdin line, one ack line per command on stdout):

* ``show`` / ``hide`` — acked by the reader thread the moment they are
  queued; the next tick (~33ms) acts on them.
* ``grab_begin`` / ``grab_end`` — acked only after the main thread has
  actually ordered the panels out/in. That ordering is the guarantee a
  screenshot capture relies on: between the parent receiving the
  ``grab_begin`` ack and sending ``grab_end``, no ring pixel exists.
* ``quit`` — clean exit; stdin EOF triggers it too, so a dead parent
  never leaves a halo behind.

Like every platform-touching module here, the AppKit imports happen inside
functions: this file imports cleanly where pyobjc does not exist, and only
``main()`` needs a Mac.

Run via ``python -m tst_cu_mcp.overlay.darwin_helper``; ``main()`` is the entry.
"""

from __future__ import annotations

import math
import queue
import sys
import threading
import time
from typing import Any

# --- geometry / motion ------------------------------------------------------

#: Edge gap, stroke widths and corner radius, in points.
INSET_POINTS = 4.0
CORE_WIDTH = 6.0
HALO_WIDTH = 16.0
CORNER_RADIUS = 18.0

#: Main-loop cadence: drains commands, drives pulse and fade.
TICK_SECONDS = 1.0 / 30.0

#: Pulse matches GlowLayer's CSS: opacity 0.65 -> 1 over 1.6s, alternating.
PULSE_PERIOD_SECONDS = 1.6
PULSE_MIN = 0.75
STATIC_ALPHA = 0.85

#: ``--color-accent`` in ui/src/lib/tokens.css and its dark variant.
LIGHT_RUST = (0.706, 0.325, 0.165)  # #b4532a
DARK_RUST = (0.816, 0.475, 0.310)  # #d0794f

#: Above every regular window including the menu bar; ABI-stable level.
SCREEN_SAVER_WINDOW_LEVEL = 1000

#: NSEventTypeApplicationDefined — the wake-up that lets stop_ unwind.
_APPLICATION_DEFINED_EVENT_TYPE = 15

_SHOW, _HIDE, _GRAB_BEGIN, _GRAB_END, _QUIT = (
    "show",
    "hide",
    "grab_begin",
    "grab_end",
    "quit",
)
#: Commands whose ack must wait for the main thread to have acted.
_MAIN_THREAD_ACKS = frozenset({_GRAB_BEGIN, _GRAB_END})
#: Reader-thread patience with the main thread before reporting failure.
_MAIN_ACK_TIMEOUT = 2.5

Command = tuple[str, threading.Event | None]


def main() -> None:
    """Build the app, wire stdin to the command queue, and run forever."""
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    # Accessory, NOT Prohibited: Prohibited registers the process with
    # LaunchServices as LSBackgroundOnly, and background-only apps are
    # forbidden from presenting UI — every panel silently fails to
    # composite. Accessory (LSUIElement) keeps us out of the Dock and
    # menu bar while still allowing windows.
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    commands: queue.Queue[Command] = queue.Queue()
    controller = _build_controller(commands)

    reader = threading.Thread(target=_read_stdin, args=(commands,), daemon=True)
    reader.start()

    # Build before running: the screen-params notification only fires on
    # change, so without this the first SHOW would find no panels at all.
    controller.rebuild_panels()
    controller.schedule_timer()
    controller.observe_screen_changes()

    app.run()


def _build_controller(commands: queue.Queue[Command]) -> Any:
    """Define the ObjC classes (needs AppKit) and return a wired controller."""
    import objc
    from AppKit import NSView
    from Foundation import NSObject

    class RingView(NSView):
        """Strokes the rust ring; the window alpha carries pulse and fade."""

        def drawRect_(self, _rect: Any) -> None:
            from AppKit import NSBezierPath
            from Foundation import NSColor, NSMakeRect

            bounds = self.bounds()
            inner = NSMakeRect(
                bounds.origin.x + INSET_POINTS,
                bounds.origin.y + INSET_POINTS,
                bounds.size.width - 2.0 * INSET_POINTS,
                bounds.size.height - 2.0 * INSET_POINTS,
            )
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                inner, CORNER_RADIUS, CORNER_RADIUS
            )
            red, green, blue = _rust()
            path.setLineWidth_(HALO_WIDTH)
            NSColor.colorWithCalibratedRed_green_blue_alpha_(red, green, blue, 0.22).setStroke()
            path.stroke()
            path.setLineWidth_(CORE_WIDTH)
            NSColor.colorWithCalibratedRed_green_blue_alpha_(red, green, blue, 0.95).setStroke()
            path.stroke()

    class GlowController(NSObject):
        """Owns the per-display panels and the state machine behind them."""

        def initWithQueue_(self, q: queue.Queue[Command]) -> Any:
            # PyObjC classes must chain up through objc.super; builtin super
            # cannot see the Objective-C instance methods.
            self = objc.super(GlowController, self).init()
            if self is None:
                return None
            self.commands = q
            # Keyed by (name, frame) so a display change rebuilds its panel.
            self.panels: dict[tuple[str, int, int, int, int], Any] = {}
            self.visible = False
            self.grab_depth = 0
            self.last_activity = 0.0
            self.fade_alpha = 0.0
            self.phase = 0.0
            self.last_tick = time.monotonic()
            return self

        # -- wiring ----------------------------------------------------------

        def schedule_timer(self) -> None:
            from AppKit import NSTimer
            from Foundation import NSRunLoop, NSRunLoopCommonModes

            timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                TICK_SECONDS, self, "tick:", None, True
            )
            # Common modes keeps ticking through menu tracking and resizes,
            # which matters because grab acks depend on this loop.
            NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)

        def observe_screen_changes(self) -> None:
            from Foundation import NSNotificationCenter

            NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                self,
                "screensChanged:",
                "NSApplicationDidChangeScreenParametersNotification",
                None,
            )

        # -- command handlers (main thread) -----------------------------------

        def tick_(self, _timer: Any) -> None:
            now = time.monotonic()
            elapsed = min(now - self.last_tick, 0.5)
            self.last_tick = now

            self._drain_commands()
            self._advance_fade(elapsed)
            self._apply_alpha(elapsed)

        def screensChanged_(self, _note: Any) -> None:
            self.rebuild_panels()

        # -- state transitions -------------------------------------------------

        def _drain_commands(self) -> None:
            while True:
                try:
                    command, ack = self.commands.get_nowait()
                except queue.Empty:
                    return
                if command == _SHOW:
                    self.visible = True
                    self.last_activity = time.monotonic()
                    if self.grab_depth == 0:
                        self._ensure_front()
                elif command == _HIDE:
                    self.visible = False
                elif command == _GRAB_BEGIN:
                    # Instant, not faded: a capture cannot wait out an ease.
                    self.grab_depth += 1
                    self._hide_panels_now()
                elif command == _GRAB_END:
                    self.grab_depth = max(0, self.grab_depth - 1)
                    if self.grab_depth == 0 and self.visible:
                        self._ensure_front()
                elif command == _QUIT:
                    self._stop_app()
                if ack is not None:
                    ack.set()

        def rebuild_panels(self) -> None:
            from AppKit import NSScreen

            def _key(screen: Any) -> tuple[str, int, int, int, int]:
                frame = screen.frame()
                return (
                    str(screen.localizedName()),
                    round(frame.origin.x),
                    round(frame.origin.y),
                    round(frame.size.width),
                    round(frame.size.height),
                )

            wanted = {_key(screen): screen for screen in NSScreen.screens()}
            for known in list(self.panels):
                if known not in wanted:
                    self.panels.pop(known).orderOut_(None)
            for key, screen in wanted.items():
                if key not in self.panels:
                    self.panels[key] = self._make_panel(screen)

        def _make_panel(self, screen: Any) -> Any:
            from AppKit import (
                NSPanel,
                NSWindowCollectionBehaviorCanJoinAllSpaces,
                NSWindowCollectionBehaviorFullScreenAuxiliary,
                NSWindowSharingNone,
                NSWindowStyleMaskBorderless,
                NSWindowStyleMaskNonactivatingPanel,
            )
            from Foundation import NSColor

            frame = screen.frame()
            # NonActivatingPanel is what lets a panel that never takes key
            # status actually join every space — including native fullscreen
            # spaces. Without it the panels composite only on ordinary
            # desktop spaces and vanish whenever a fullscreen app is front.
            panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                frame,
                NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
                2,
                False,
            )
            panel.setLevel_(SCREEN_SAVER_WINDOW_LEVEL)
            panel.setIgnoresMouseEvents_(True)
            # Invisible to screen capture: CGWindowList and ScreenCaptureKit
            # skip non-shareable windows, so a grab from any process — not
            # only this one — never contains the ring. grab_begin/grab_end
            # stays as the belt for the same-process path.
            panel.setSharingType_(NSWindowSharingNone)
            panel.setOpaque_(False)
            panel.setBackgroundColor_(NSColor.clearColor())
            panel.setHasShadow_(False)
            panel.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorFullScreenAuxiliary
            )
            panel.setContentView_(RingView.alloc().initWithFrame_(frame))
            panel.setAlphaValue_(0.0)
            return panel

        def _ensure_front(self) -> None:
            # orderFrontRegardless, not makeKeyAndOrderFront: the glow may
            # never take focus or interrupt whatever the user is doing.
            for panel in self.panels.values():
                panel.orderFrontRegardless()

        def _hide_panels_now(self) -> None:
            self.fade_alpha = 0.0
            for panel in self.panels.values():
                panel.setAlphaValue_(0.0)
                panel.orderOut_(None)

        def _advance_fade(self, elapsed: float) -> None:
            target = 1.0 if (self.visible and self.grab_depth == 0) else 0.0
            rate = 6.0 if target > self.fade_alpha else 3.0  # per second
            step = (target - self.fade_alpha) * min(1.0, rate * elapsed)
            self.fade_alpha = max(0.0, min(1.0, self.fade_alpha + step))
            if target == 0.0 and self.fade_alpha < 0.02 and self.panels:
                for panel in self.panels.values():
                    panel.orderOut_(None)

        def _apply_alpha(self, elapsed: float) -> None:
            if self.fade_alpha <= 0.0:
                return
            self.phase += elapsed * (2.0 * math.pi / PULSE_PERIOD_SECONDS)
            pulse = (
                STATIC_ALPHA
                if _reduce_motion()
                else PULSE_MIN + (1.0 - PULSE_MIN) * (0.5 + 0.5 * math.sin(self.phase))
            )
            alpha = self.fade_alpha * pulse
            for panel in self.panels.values():
                panel.setAlphaValue_(alpha)

        def _stop_app(self) -> None:
            from AppKit import NSApplication, NSEvent
            from Foundation import NSMakePoint

            app = NSApplication.sharedApplication()
            app.stop_(None)
            # stop_ only unwinds once an event flows; post the wake-up.
            wake = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(  # noqa: E501
                _APPLICATION_DEFINED_EVENT_TYPE,
                NSMakePoint(0.0, 0.0),
                0,
                0.0,
                0,
                None,
                0,
                0,
                0,
            )
            app.postEvent_atStart_(wake, True)

    return GlowController.alloc().initWithQueue_(commands)


def _rust() -> tuple[float, float, float]:
    from AppKit import NSApplication

    try:
        appearance = str(NSApplication.sharedApplication().effectiveAppearance().name())
    except Exception:
        return LIGHT_RUST
    return DARK_RUST if "Dark" in appearance else LIGHT_RUST


def _reduce_motion() -> bool:
    from AppKit import NSWorkspace

    try:
        return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
    except Exception:
        return False


def _read_stdin(commands: queue.Queue[Command]) -> None:
    """Reader thread: enqueue lines, hand back acks.

    ``show``/``hide`` ack immediately — they are queued and the tick will
    act within ~33ms. The grab pair waits for the main thread's event, so
    the parent's capture ordering holds. EOF means the parent is gone:
    quit rather than haunt the screen.
    """
    try:
        for line in sys.stdin:
            command = line.strip()
            if not command:
                continue
            if command in _MAIN_THREAD_ACKS:
                ack: threading.Event = threading.Event()
                commands.put((command, ack))
                print("ok" if ack.wait(_MAIN_ACK_TIMEOUT) else "err", flush=True)
            elif command == _QUIT:
                commands.put((_QUIT, None))
                return
            else:
                commands.put((command, None))
                print("ok", flush=True)
    except Exception:
        pass
    finally:
        commands.put((_QUIT, None))


if __name__ == "__main__":
    main()
