"""When the ring and the agent cursors show, and where (TD-5226, TD-5227).

Painters (darwin, win32, linux) only light, darken and aim. This controller
owns the rules:

* **Mode.** The ring means "the agent has the real screen". In background
  mode (``mode: background`` in the policy file) agents work through
  accessibility calls and never take the screen, so the ring stays off on
  every display, even when such an agent falls back to a coordinate tool.
  Only ``full_control`` lights it. The agent cursor shows in both modes.
* **Switches.** ``overlay.enabled`` (TST Desk's "show on the real display")
  gates both; ``overlay.cursor`` (its "agent cursor") gates the cursor. The
  file is re-read when it changes, so Settings applies to every running
  client without a restart.
* **Display.** Only the display being acted on is lit: the one the last
  capture targeted, or the one under the last pointer action. Before
  either, the painter's default (the primary display). Painters that cannot
  light a single display (Windows and Linux today) ring every display.
* **Episodes.** TST Desk brackets each session's computer use with
  ``overlay_session``; the ring stays up while any session's episode is
  open. A client that never brackets lights the ring per action, and it
  fades :data:`UNBRACKETED_LINGER_SECONDS` after the last one.
* **Cursors.** Each agent's cursor goes to its last move, click or acted-on
  element; the painter fades it when that agent goes idle. It never touches
  the hardware pointer.
* **Stop.** While the kill-switch is engaged the ring and every cursor are
  dark. A watcher re-checks every :data:`WATCH_SECONDS`, so Stop clears them
  without waiting for the agent's next action.

The state is level-triggered: each entry point and each watcher tick works
out what is wanted and talks to the painter only on a change. Painter calls
happen under one lock because the watcher thread and the tool thread both
reach the painter. The package's failure rule holds: nothing here raises
into a tool.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from tst_cu_mcp.config import Config
from tst_cu_mcp.overlay.agents import Agent, AgentBook

if TYPE_CHECKING:
    from tst_cu_mcp.overlay import Painter

#: How often the watcher re-checks Stop, the policy file and the linger.
WATCH_SECONDS = 0.25
#: A non-bracketing client's ring fades this long after its last action.
UNBRACKETED_LINGER_SECONDS = 30.0
#: The mode that lights the ring.
FULL_CONTROL = "full_control"


class PolicyFile:
    """The policy file as a :class:`Config`, re-parsed only when it changes.

    No file means the installed config (permissive defaults: background
    mode, overlay and cursor on). A file that fails to parse reads as
    background with the overlay off, so a broken edit darkens rather than
    lights.
    """

    def __init__(self, path: Callable[[], Path] | None = None) -> None:
        from tst_cu_mcp.config import policy_path

        self._path = path or policy_path
        self._stamp: tuple[float, int] | None = None
        self._config = Config(overlay_enabled=False)

    def __call__(self) -> Config:
        from tst_cu_mcp import safety
        from tst_cu_mcp.config import load_config

        target = self._path()
        try:
            stat = target.stat()
        except OSError:
            self._stamp = None
            return safety.active_config()
        stamp = (stat.st_mtime, stat.st_size)
        if stamp != self._stamp:
            self._stamp = stamp
            try:
                self._config = load_config(target)
            except Exception:
                self._config = Config(overlay_enabled=False)
        return self._config


def _killswitch() -> bool:
    from tst_cu_mcp import safety

    try:
        return safety.killswitch_engaged()
    except Exception:
        return True


class GatedOverlay:
    """An :class:`~tst_cu_mcp.overlay.Overlay` that applies the rules."""

    def __init__(
        self,
        painter: Painter,
        *,
        policy: Callable[[], Config] | None = None,
        stopped: Callable[[], bool] = _killswitch,
        clock: Callable[[], float] = time.monotonic,
        linger: float = UNBRACKETED_LINGER_SECONDS,
        agents: AgentBook | None = None,
        watch: bool = True,
    ) -> None:
        self.painter = painter
        self.name = painter.name
        self.agents = agents or AgentBook()
        self._policy = policy or PolicyFile()
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
        self._cursors_out = False
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

    def use_agent(self, key: str, label: str = "") -> Agent:
        """Name the agent whose actions follow (TST Desk's shared sidecar)."""
        with self._lock:
            return self.agents.use(key, label)

    def pointer(self, x: float, y: float, *, click: bool = False) -> None:
        """The acting agent's cursor goes to this global point (points)."""
        with self._lock:
            try:
                if not self._cursor_wanted():
                    return
                self.painter.cursor(self.agents.current(), float(x), float(y), click)
                self._cursors_out = True
                self._start_watcher()
            except Exception:
                return

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

    def prewarm(self) -> None:
        """Start a slow-starting painter now, off the tool path (TD-5230)."""
        warm = getattr(self.painter, "prewarm", None)
        if callable(warm):
            with contextlib.suppress(Exception):
                warm()

    def shutdown(self) -> None:
        self._halt.set()
        watcher = self._watcher
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=1.0)
        with self._lock, contextlib.suppress(Exception):
            self.painter.shutdown()
            self.agents.close()

    # -- rules ------------------------------------------------------------

    def wanted(self) -> bool:
        """Whether the ring should be lit right now."""
        if self._blocked or self._stopped():
            return False
        policy = self._policy()
        if not policy.overlay_enabled or policy.mode != FULL_CONTROL:
            return False
        if self._episodes:
            return True
        if self._bracketing or self._last_activity is None:
            return False
        return self._clock() - self._last_activity < self._linger

    def _cursor_wanted(self) -> bool:
        if self._stopped():
            return False
        policy = self._policy()
        return policy.overlay_enabled and policy.cursor_enabled

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
            if self._cursors_out and not self._cursor_wanted():
                self.painter.cursor_hide(None)
                self._cursors_out = False
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
    "PolicyFile",
]
