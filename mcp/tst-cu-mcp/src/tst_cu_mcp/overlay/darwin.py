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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Protocol

from tst_cu_mcp.overlay import (
    CMD_GRAB_BEGIN,
    CMD_GRAB_END,
    CMD_HIDE,
    CMD_QUIT,
    CMD_SHOW,
)

#: ``show``/``hide`` are acked by the helper's reader thread on arrival.
FAST_ACK_TIMEOUT = 1.0
#: ``grab_*`` wait for the helper's main thread to have acted (a tick is
#: ~33ms; the budget covers helper startup and a busy runloop).
GRAB_ACK_TIMEOUT = 3.0


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
    """Start the helper: same interpreter, so the same pyobjc-carrying venv."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "tst_cu_mcp.overlay.darwin_helper"],
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

    def _send(self, command: str, *, timeout: float = FAST_ACK_TIMEOUT) -> bool:
        if self.degraded:
            return False
        try:
            if self._transport is None:
                self._transport = self._spawn()
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

    def activity(self) -> None:
        from tst_cu_mcp import safety

        if safety.killswitch_engaged():
            self.notify_blocked()
            return
        self._send(CMD_SHOW)

    def notify_blocked(self) -> None:
        self._send(CMD_HIDE)

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
