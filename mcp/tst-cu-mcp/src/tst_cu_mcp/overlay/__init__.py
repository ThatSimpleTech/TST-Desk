"""The real-display glow: signaling, never input.

While an agent drives the real screen (full-control mode), a rust ring glows
around the edges of the display it is acting on — the machine itself saying
"the agent is driving". :mod:`tst_cu_mcp.overlay.control` holds the rules
(mode, target display, episodes, idle fade, Stop); the platform painters
below only light and darken. The overlay is owned by this
sidecar because the sidecar is what actuates: keeping it here makes the
hide-for-capture ordering two function calls in one process instead of a
cross-process race, so a screenshot can never contain the glow it just
hid.

Platform resolution mirrors :mod:`tst_cu_mcp.backends`: a real
painter on darwin, win32, and Linux X11, wrapped in the rules controller.
Wayland stays a no-op (TD-2002). Unlike backends, the resolved instance is cached — some
painters own a helper or a pulse thread that must not be spawned per
call. Tests install fakes with :func:`set_overlay` and clear with
:func:`reset_overlay`.

Two failure rules shape the API:

* **An overlay problem must never break the tools.** Every entry point
  swallows its own failures and degrades — permanently — to the null
  overlay. Actuation and capture behave exactly as before, just unlit.
* **The glow follows the kill-switch.** A refused action is announced as
  blocked, and activity while the switch is engaged hides the glow rather
  than lighting it. Stopping the hands also stops the halo.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

#: Env override resolved before the config file. The daemon sets this from
#: the user's ``show_on_real_display`` pref when it spawns the sidecar, so
#: an explicit ``0``/``1`` here is the user's answer, not a default.
OVERLAY_ENV = "TST_CU_MCP_OVERLAY"

# Helper line protocol. SHOW/HIDE are acked on arrival; the GRAB pair is
# acked by the helper's main thread *after* the panels are actually
# hidden/shown, which is what gives a capture its ordering guarantee.
CMD_SHOW = "show"
CMD_HIDE = "hide"
CMD_TARGET = "target"
CMD_GRAB_BEGIN = "grab_begin"
CMD_GRAB_END = "grab_end"
CMD_QUIT = "quit"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


class Overlay(Protocol):
    """What the actuation and capture paths need from a glow."""

    name: str

    def begin_session(self, key: str = "") -> None:
        """A client session's computer-use episode opened."""
        ...

    def end_session(self, key: str = "") -> None:
        """That episode closed."""
        ...

    def activity(self) -> None:
        """An actuation is about to run."""
        ...

    def notify_blocked(self) -> None:
        """Actuation was refused: the glow must not be lit."""
        ...

    def set_target(self, display_id: int | None) -> None:
        """The display being acted on (a display id from :mod:`displays`)."""
        ...

    @contextmanager
    def grab_hidden(self) -> Iterator[None]:
        """Hide the glow for the duration of one screenshot grab."""
        yield

    def shutdown(self) -> None:
        """Release whatever the overlay is holding (helper process, ...)."""
        ...


class Painter(Protocol):
    """A platform ring: light it, darken it, aim it. No rules of its own."""

    name: str

    def begin_session(self) -> None: ...

    def end_session(self) -> None:
        """Darken the ring."""
        ...

    def activity(self) -> None:
        """Light the ring (idempotent)."""
        ...

    def notify_blocked(self) -> None: ...

    def set_target(self, display_id: int | None) -> None:
        """Light this display only, where the platform can; else a no-op."""
        ...

    @contextmanager
    def grab_hidden(self) -> Iterator[None]:
        yield

    def shutdown(self) -> None: ...


class NullOverlay:
    """The honest no-op: unsupported platforms and degraded states."""

    name = "null"

    def begin_session(self, key: str = "") -> None:
        return None

    def end_session(self, key: str = "") -> None:
        return None

    def activity(self) -> None:
        return None

    def notify_blocked(self) -> None:
        return None

    def set_target(self, display_id: int | None) -> None:
        return None

    @contextmanager
    def grab_hidden(self) -> Iterator[None]:
        yield

    def shutdown(self) -> None:
        return None


def overlay_enabled(platform: str | None = None) -> bool:
    """Whether the real-display glow should run at all.

    ``TST_CU_MCP_OVERLAY`` beats the config file's ``overlay.enabled``,
    which beats the platform default (on for darwin / win32 / Linux X11;
    off on Wayland, where there is no painter).
    """
    from tst_cu_mcp import safety

    raw = os.environ.get(OVERLAY_ENV, "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    if not safety.active_config().overlay_enabled:
        return False
    target = sys.platform if platform is None else platform
    if target.startswith("linux") and _wayland_desktop():
        return False
    return target in {"darwin", "win32"} or target.startswith("linux")


def _wayland_desktop() -> bool:
    """True when this process is on a Wayland session (env only)."""
    session = os.environ.get("XDG_SESSION_TYPE", "").strip().casefold()
    if session == "wayland":
        return True
    return bool(os.environ.get("WAYLAND_DISPLAY", "").strip()) and session != "x11"


_overlay: Overlay | None = None


def set_overlay(overlay: Overlay | None) -> None:
    """Install an overlay, overriding resolution. Tests only."""
    global _overlay
    _overlay = overlay


def reset_overlay() -> None:
    """Drop the cached overlay; the next call re-resolves. Tests only."""
    set_overlay(None)


def get_overlay() -> Overlay:
    """The process overlay, resolved once and cached. Never raises."""
    global _overlay
    if _overlay is None:
        if not overlay_enabled():
            _overlay = NullOverlay()
            return _overlay
        try:
            from tst_cu_mcp.overlay.control import GatedOverlay

            _overlay = GatedOverlay(_resolve_painter())
        except Exception:
            _overlay = NullOverlay()
    return _overlay


def _resolve_painter() -> Painter:
    """The platform painter. Import-time OS work stays inside each module."""
    target = sys.platform
    if target == "darwin":
        from tst_cu_mcp.overlay.darwin import DarwinOverlay

        return DarwinOverlay()
    if target == "win32":
        from tst_cu_mcp.overlay.win32 import Win32Overlay

        return Win32Overlay()
    if target.startswith("linux"):
        from tst_cu_mcp.overlay.linux import LinuxOverlay

        return LinuxOverlay()
    return NullOverlay()
