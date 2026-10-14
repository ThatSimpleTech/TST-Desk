"""Per-computer state and the per-viewer lane (TD-5015).

A lane is one viewer connection's own socket to one paired computer. It
rewrites session ids both ways and keeps the other computer's meaning
for that one connection: replies arrive in order, attach and replay work
as they do locally, and seq passes through untouched.

The lane tracks the last seq it forwarded per session. After an upstream
drop, or a re-pairing that swaps its socket (:meth:`Lane.relink`), it
re-attaches from one past it, so the viewer sees no gap and no
duplicate. Verbs sent while it connects wait (10 s, 64 at most) and then
fail with ``machine_unreachable`` naming their session; so do any still
waiting when the lane closes.

A lane made while its computer is parked (refused, locked, outdated)
does not dial: it only remembers what the viewer attached, and dials
when the computer comes back (:meth:`Lane.wake`).
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Protocol

from .bridge_link import UpstreamLink
from .bridge_machine import MachineState, Timings
from .bridge_wire import bridged_id, rewrite_from_remote
from .protocol import scrub_wire_json

# Session events that can change a row (state, busy, updated_at, title).
ROW_EVENTS = frozenset({"session_state", "turn_complete", "approval_request", "user_turn"})


class LaneHost(Protocol):
    """What a lane needs from the bridge that owns it."""

    clock: Callable[[], float]
    timings: Timings

    def new_link(
        self,
        machine: MachineState,
        role: str,
        on_open: Callable[[], Awaitable[None]],
        on_frame: Callable[[dict[str, Any]], Awaitable[None]],
        on_status: Callable[[], None] | None,
    ) -> UpstreamLink: ...

    def unreachable(self, machine: MachineState, session_id: str) -> str: ...

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None: ...

    async def deliver(self, viewer: Any, payload: str) -> None: ...

    def set_rows(
        self, machine: MachineState, rows: list[dict[str, Any]], requester: Any
    ) -> None: ...


class Lane:
    """One viewer connection's socket to one computer."""

    def __init__(
        self, bridge: LaneHost, viewer: Any, machine: MachineState, *, dial: bool = True
    ) -> None:
        self._bridge = bridge
        self.viewer = viewer
        self.machine = machine
        self.attached: dict[str, int] = {}
        self.last_seq: dict[str, int] = {}
        self.pending: deque[tuple[float, dict[str, Any]]] = deque()
        self.touched = bridge.clock()
        self._expiry: asyncio.TimerHandle | None = None
        self._closed = False
        self.link = self._new_link()
        if dial:
            self.link.start()

    def _new_link(self) -> UpstreamLink:
        return self._bridge.new_link(self.machine, "lane", self._on_open, self._on_frame, None)

    @property
    def is_open(self) -> bool:
        return self.link.is_open

    def idle(self, now: float) -> bool:
        return (
            not self.attached
            and not self.pending
            and now - self.touched >= self._bridge.timings.lane_idle_seconds
        )

    def send(self, upstream: dict[str, Any]) -> None:
        self.touched = self._bridge.clock()
        self.link.send(json.dumps(upstream))

    def attach(self, raw: str, upstream: dict[str, Any]) -> None:
        from_seq = upstream.get("from_seq", 1)
        asked = from_seq if isinstance(from_seq, int) and from_seq >= 1 else 1
        self.attached[raw] = asked
        self.last_seq[raw] = asked - 1
        if self.is_open:
            self.send(upstream)

    def detach(self, raw: str, upstream: dict[str, Any]) -> None:
        self.attached.pop(raw, None)
        self.last_seq.pop(raw, None)
        if self.is_open:
            self.send(upstream)

    def enqueue(self, upstream: dict[str, Any], session_id: str) -> str | None:
        """Hold one verb until the lane is up. An error frame when full."""
        if len(self.pending) >= self._bridge.timings.queue_limit:
            return self._bridge.unreachable(self.machine, session_id)
        self.pending.append((self._bridge.clock(), upstream))
        self.touched = self._bridge.clock()
        if self._expiry is None:
            self._expiry = asyncio.get_running_loop().call_later(
                self._bridge.timings.queue_seconds, self._expire
            )
        return None

    def _expire(self) -> None:
        self._expiry = None
        now = self._bridge.clock()
        while self.pending and now - self.pending[0][0] >= self._bridge.timings.queue_seconds:
            self._fail(self.pending.popleft()[1])
        if self.pending:
            wait = self._bridge.timings.queue_seconds - (now - self.pending[0][0])
            self._expiry = asyncio.get_running_loop().call_later(max(wait, 0.01), self._expire)

    def _fail(self, upstream: dict[str, Any]) -> None:
        """Answer one verb that never went: ``machine_unreachable``, same session."""
        sid = bridged_id(str(upstream.get("session_id", "")), self.machine.mch)
        self._bridge.spawn(
            self._bridge.deliver(self.viewer, self._bridge.unreachable(self.machine, sid))
        )

    async def _on_open(self) -> None:
        # Re-attach first, from one past the last frame the viewer got, so
        # an upstream drop leaves no gap and no duplicate; then the backlog.
        for raw, asked in self.attached.items():
            from_seq = self.last_seq.get(raw, asked - 1) + 1
            self.link.send(json.dumps({"type": "attach", "session_id": raw, "from_seq": from_seq}))
        while self.pending:
            _, upstream = self.pending.popleft()
            self.link.send(json.dumps(upstream))
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        self.touched = self._bridge.clock()

    async def _on_frame(self, frame: dict[str, Any]) -> None:
        out = rewrite_from_remote(frame, self.machine.mch, self.machine.name)
        if out is None:
            return
        self.touched = self._bridge.clock()
        kind = out.get("type")
        if kind == "session_list":
            self._bridge.set_rows(self.machine, out.get("sessions") or [], self.viewer)
            return
        raw, seq = frame.get("session_id"), frame.get("seq")
        if isinstance(raw, str) and isinstance(seq, int) and not isinstance(seq, bool):
            self.last_seq[raw] = max(self.last_seq.get(raw, 0), seq)
        await self._bridge.deliver(self.viewer, scrub_wire_json(json.dumps(out)))
        if kind in ROW_EVENTS:
            self.machine.debounce_refresh()

    @property
    def dormant(self) -> bool:
        """Made while its computer was parked, and not dialing yet."""
        return not self._closed and not self.link.running

    def wake(self) -> None:
        """Dial now: the computer is back, or "Try now"."""
        if self._closed:
            return
        self.link.start()
        self.link.reset_backoff()

    async def relink(self) -> None:
        """A new socket to the computer's current address and key (re-pairing).

        What the viewer attached and the seq it reached stay, so the new
        socket re-attaches from one past it; queued verbs wait for it.
        """
        if self._closed:
            return
        old, self.link = self.link, self._new_link()
        await old.stop()
        if not self._closed:
            self.link.start()

    async def stop(self) -> None:
        """Close for good. Verbs still waiting hear ``machine_unreachable``."""
        self._closed = True
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        while self.pending:
            self._fail(self.pending.popleft()[1])
        await self.link.stop()
