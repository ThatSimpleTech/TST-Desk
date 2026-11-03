"""Darwin glow manager: a helper child paints, this side just talks to it.

AppKit wants a runloop; the MCP server's loop is asyncio over stdio. Rather
than interleave the two in one process, the manager spawns
``python -m tst_cu_mcp.overlay.darwin_helper`` (same interpreter, so the same venv
that already carries pyobjc) and drives it with a line protocol over stdin:

* ``show`` / ``hide`` are acked on arrival — fire-and-forget semantics are
  fine for the glow's on/off state, the linger timer smooths the rest.
* ``grab_begin`` / ``grab_end`` are acked by the helper's main thread only
  after the panels actually left the screen (or came back), which is the
  ordering a capture needs to stay clean.
* ``quit`` ends the helper; stdin EOF does too, so a crashed sidecar never
  leaves an orphaned halo behind.

Start-up (TD-5230): in the packaged app the helper is the frozen ``tstd``,
which unpacks itself before Python starts, so its first ack takes seconds
(2-5 s measured), not milliseconds. The first command after a spawn is a
``hide`` probe allowed :data:`STARTUP_ACK_TIMEOUT`; the server starts the
helper in the background at launch (:meth:`DarwinOverlay.prewarm`) so the
agent's first action rarely waits for it. Before this, the 1 s ack budget
expired on almost every cold start and the overlay switched itself off for
the life of the process, so the packaged app often showed no ring at all.

Failure rule (see the package docstring): any spawn, write, or ack trouble
degrades this manager — permanently — to a no-op. A capture whose grab ack
timed out still proceeds; worst case one frame contains the ring, and the
glow switches itself off rather than risk a second one.
"""

from __future__ import annotations

import contextlib
import select
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Protocol

from tst_cu_mcp.overlay import (
    CMD_CURSOR,
    CMD_CURSOR_HIDE,
    CMD_GRAB_BEGIN,
    CMD_GRAB_END,
    CMD_HIDE,
    CMD_QUIT,
    CMD_SHOW,
    CMD_TARGET,
)
from tst_cu_mcp.overlay.agents import Agent, clean_key, clean_label

#: ``show``/``hide`` are acked by the helper's reader thread on arrival.
FAST_ACK_TIMEOUT = 1.0
#: ``grab_*`` wait for the helper's main thread to have acted (a tick is
#: ~33ms; the budget covers a busy runloop).
GRAB_ACK_TIMEOUT = 3.0
#: The first ack after a spawn: a frozen helper unpacks itself first.
STARTUP_ACK_TIMEOUT = 15.0


class Transport(Protocol):
    """One live helper connection."""

    def send(self, command: str, *, timeout: float = FAST_ACK_TIMEOUT) -> bool:
        """Send one command, wait for its ack. True only on a real ack."""
        ...

    def close(self) -> None:
        """Best-effort shutdown of the child."""
        ...


class PipeTransport:
    """stdin/stdout line protocol with select-based ack timeouts.

    A hung helper must never hang an actuation or a capture, so every read
    is bounded by :func:`select.select` rather than trusting the pipe.
    """

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc

    def send(self, command: str, *, timeout: float = FAST_ACK_TIMEOUT) -> bool:
        stdin, stdout = self._proc.stdin, self._proc.stdout
        if stdin is None or stdout is None:
            return False
        try:
            stdin.write(command + "\n")
            stdin.flush()
            ready, _, _ = select.select([stdout], [], [], timeout)
            if not ready:
                return False
            ack_line: str = stdout.readline()
            return ack_line.strip() == "ok"
        except (BrokenPipeError, OSError, ValueError):
            return False

    def close(self) -> None:
        try:
            if self._proc.stdin is not None and not self._proc.stdin.closed:
                self._proc.stdin.write(CMD_QUIT + "\n")
                self._proc.stdin.flush()
                self._proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
        try:
            self._proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        except OSError:
            pass


def default_spawn() -> Transport:
    """Start the helper: same interpreter, so the same pyobjc-carrying venv.

    Frozen (TD-4834): ``sys.executable`` is the ``tstd`` onefile, which has
    no ``-m`` — re-exec it with ``--cu-overlay`` instead (the flag branch
    lives in ``tstd.daemon.main``).
    """
    if getattr(sys, "frozen", False):
        argv = [sys.executable, "--cu-overlay"]
    else:
        argv = [sys.executable, "-m", "tst_cu_mcp.overlay.darwin_helper"]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    return PipeTransport(proc)


class DarwinOverlay:
    """The :class:`Overlay` implementation for macOS; helper-backed."""

    name = "darwin"

    def __init__(self, spawn: Callable[[], Transport] | None = None) -> None:
        self._spawn = spawn or default_spawn
        self._transport: Transport | None = None
        self.degraded = False
        # Serializes the prewarm thread with commands from the tool thread.
        self._lock = threading.Lock()

    def prewarm(self) -> None:
        """Start the helper in the background so the first action finds it up."""
        if self.degraded or self._transport is not None:
            return
        threading.Thread(target=self._prewarm, name="overlay-prewarm", daemon=True).start()

    def _prewarm(self) -> None:
        with self._lock:
            if self.degraded or self._transport is not None:
                return
            try:
                self._transport = self._connect()
            except Exception:
                self._degrade()

    def _connect(self) -> Transport:
        """Spawn the helper and wait, generously, for its first ack."""
        transport = self._spawn()
        if not transport.send(CMD_HIDE, timeout=STARTUP_ACK_TIMEOUT):
            with contextlib.suppress(Exception):
                transport.close()
            raise RuntimeError("overlay helper did not start")
        return transport

    def _send(self, command: str, *, timeout: float = FAST_ACK_TIMEOUT) -> bool:
        with self._lock:
            if self.degraded:
                return False
            try:
                if self._transport is None:
                    self._transport = self._connect()
                if self._transport.send(command, timeout=timeout):
                    return True
            except Exception:
                pass
            self._degrade()
            return False

    def _degrade(self) -> None:
        """Give up on the glow for the life of the process."""
        self.degraded = True
        transport, self._transport = self._transport, None
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()

    def begin_session(self) -> None:
        self.activity()

    def end_session(self) -> None:
        self._send(CMD_HIDE)

    def activity(self) -> None:
        from tst_cu_mcp import safety

        if safety.killswitch_engaged():
            self.notify_blocked()
            return
        self._send(CMD_SHOW)

    def notify_blocked(self) -> None:
        self._send(CMD_HIDE)

    def set_target(self, display_id: int | None) -> None:
        if display_id is not None and display_id >= 0:
            self._send(f"{CMD_TARGET} {int(display_id)}")

    def cursor(self, agent: Agent, x: float, y: float, click: bool) -> None:
        # One line: key, point (global, top-left origin), fill, click, label.
        self._send(
            f"{CMD_CURSOR} {clean_key(agent.key)} {x:.1f} {y:.1f} {agent.color} "
            f"{1 if click else 0} {clean_label(agent.label)}"
        )

    def cursor_hide(self, key: str | None) -> None:
        self._send(f"{CMD_CURSOR_HIDE} {'*' if key is None else clean_key(key)}")

    @contextmanager
    def grab_hidden(self) -> Iterator[None]:
        hidden = self._send(CMD_GRAB_BEGIN, timeout=GRAB_ACK_TIMEOUT)
        try:
            yield
        finally:
            if hidden:
                self._send(CMD_GRAB_END, timeout=GRAB_ACK_TIMEOUT)

    def shutdown(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()


__all__ = ["DarwinOverlay", "PipeTransport", "Transport", "default_spawn"]
