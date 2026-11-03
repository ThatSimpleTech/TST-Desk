"""When the ring shows and where, for every painter (TD-5226).

Painters (darwin, win32, linux) only know how to light and darken a ring.
This controller owns the rules:

* **Mode.** The ring means "the agent has the real screen". In background
  mode (``mode: background`` in the policy file) agents work through
  accessibility calls and never take the screen, so the ring stays off on
  every display, even when such an agent falls back to a coordinate tool.
  Only ``full_control`` lights it. The file is re-read when it changes, so
  Settings applies without restarting any client.
* **Display.** Only the display being acted on is lit: the one the last
  capture targeted, or the one under the last pointer action. Before
  either, the painter's default (the primary display). Painters that cannot
  light a single display (Windows and Linux today) ring every display.
* **Episodes.** TST Desk brackets each session's computer use with
  ``overlay_session``; the ring stays up while any session's episode is
  open, so one session finishing does not darken another's ring. A client
  that never brackets (any other MCP client) lights the ring on activity,
  and it fades after :data:`UNBRACKETED_LINGER_SECONDS` without one.
* **Stop.** While the kill-switch is engaged the ring is dark. A watcher
  checks every :data:`WATCH_SECONDS`, so Stop clears the ring without
  waiting for the agent's next action.

The state is level-triggered: each entry point and each watcher tick works
out whether the ring is wanted and talks to the painter only when that, or
the target display, changes. Painter calls happen under one lock because
the watcher thread and the tool thread both reach the painter. The failure
rule of the package holds: nothing here may raise into a tool.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tst_cu_mcp.overlay import Painter

#: How often the watcher re-checks Stop, the mode and the linger.
WATCH_SECONDS = 0.25
#: A non-bracketing client's ring fades this long after its last action.
UNBRACKETED_LINGER_SECONDS = 30.0
#: The mode that lights the ring.
FULL_CONTROL = "full_control"


class PolicyMode:
    """The policy file's ``mode``, re-parsed only when the file changes.

    No file means the installed config (permissive defaults: background).
    A file that fails to parse keeps the last good answer, or background,
    so a broken edit darkens the ring rather than lighting it.
    """

    def __init__(self, path: Callable[[], Path] | None = None) -> None:
        from tst_cu_mcp.config import policy_path

        self._path = path or policy_path
        self._stamp: tuple[float, int] | None = None
        self._mode = "background"

    def __call__(self) -> str:
        from tst_cu_mcp import safety
        from tst_cu_mcp.config import load_config

        target = self._path()
        try:
            stat = target.stat()
        except OSError:
            self._stamp = None
            return safety.active_config().mode
        stamp = (stat.st_mtime, stat.st_size)
        if stamp != self._stamp:
            self._stamp = stamp
            try:
                self._mode = load_config(target).mode
            except Exception:
                self._mode = "background"
        return self._mode


def _killswitch() -> bool:
    from tst_cu_mcp import safety

    try:
        return safety.killswitch_engaged()
    except Exception:
        return True


class GatedOverlay:
    """An :class:`~tst_cu_mcp.overlay.Overlay` that applies the ring's rules."""

    def __init__(
        self,
        painter: Painter,
        *,
        mode: Callable[[], str] | None = None,
        stopped: Callable[[], bool] = _killswitch,
        clock: Callable[[], float] = time.monotonic,
        linger: float = UNBRACKETED_LINGER_SECONDS,
        watch: bool = True,
    ) -> None:
        self.painter = painter
        self.name = painter.name
        self._mode = mode or PolicyMode()
        self._stopped = stopped
        self._clock = clock
        self._linger = linger
        self._watch = watch
        self._lock = threading.RLock()
        self._episodes: set[str] = set()
        self._bracketing = False
        self._blocked = False
        self._last_activity: float | None = None
        self._target: int | None = None
        self._sent_target: int | None = None
        self._shown = False
        self._watcher: threading.Thread | None = None
        self._halt = threading.Event()

    # -- the Overlay protocol ---------------------------------------------

    def begin_session(self, key: str = "") -> None:
        """Open one client session's episode; the ring holds while any is open."""
        with self._lock:
            self._bracketing = True
            self._episodes.add(key)
            self._blocked = False
            self._sync()
            self._start_watcher()

    def end_session(self, key: str = "") -> None:
        """Close one episode. The ring goes dark when the last one closes."""
        with self._lock:
            self._episodes.discard(key)
            self._sync()

    def activity(self) -> None:
        """An actuation is about to run."""
        with self._lock:
            if not self._stopped():
                self._blocked = False
                self._last_activity = self._clock()
            self._sync()
            self._start_watcher()

    def notify_blocked(self) -> None:
        """An actuation was refused: dark until the next one goes through."""
        with self._lock:
            self._blocked = True
            self._sync()

    def set_target(self, display_id: int | None) -> None:
        """The display being acted on (the last capture's, or under the pointer)."""
        with self._lock:
            if display_id is not None:
                self._target = display_id
            self._sync()

    @contextmanager
    def grab_hidden(self) -> Iterator[None]:
        """Hide the painter for one capture, holding the lock only at the edges."""
        with self._lock:
            grab = self.painter.grab_hidden()
            grab.__enter__()
        try:
            yield
        finally:
            with self._lock:
                grab.__exit__(None, None, None)

    def shutdown(self) -> None:
        self._halt.set()
        watcher = self._watcher
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=1.0)
        with self._lock, contextlib.suppress(Exception):
            self.painter.shutdown()

    # -- rules ------------------------------------------------------------

    def wanted(self) -> bool:
        """Whether the ring should be lit right now."""
        if self._blocked or self._stopped():
            return False
        if self._mode() != FULL_CONTROL:
            return False
        if self._episodes:
            return True
        if self._bracketing or self._last_activity is None:
            return False
        return self._clock() - self._last_activity < self._linger

    def tick(self) -> None:
        """One watcher pass. Public so tests drive it without a thread."""
        with self._lock:
            self._sync()

    def _sync(self) -> None:
        try:
            if self._stopped():
                # Stop ends an unbracketed client's claim on the ring: after
                # Resume it takes a new action to light it again.
                self._last_activity = None
            wanted = self.wanted()
            if wanted and self._target is not None and self._target != self._sent_target:
                self.painter.set_target(self._target)
                self._sent_target = self._target
            if wanted and not self._shown:
                self.painter.activity()
                self._shown = True
            elif not wanted and self._shown:
                self.painter.end_session()
                self._shown = False
        except Exception:
            # The painter degrades itself; the rules never raise into a tool.
            self._shown = False

    def _start_watcher(self) -> None:
        if not self._watch or self._watcher is not None:
            return
        self._watcher = threading.Thread(target=self._watch_loop, name="overlay-watch", daemon=True)
        self._watcher.start()

    def _watch_loop(self) -> None:
        while not self._halt.wait(WATCH_SECONDS):
            self.tick()


__all__ = [
    "FULL_CONTROL",
    "UNBRACKETED_LINGER_SECONDS",
    "WATCH_SECONDS",
    "GatedOverlay",
    "PolicyMode",
]
