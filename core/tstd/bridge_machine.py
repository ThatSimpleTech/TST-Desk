"""One paired computer as the hub sees it, with its control link (TD-5015).

The control link runs while a viewer is connected. It carries presence
and last-seen, asks for the computer's own ``session_list`` (every 30 s,
on connect, and 1 s after a row event on a lane), and carries approvals
sent while no lane is open. Answers on one socket arrive in send order,
so ``routes`` says who each ``session_list`` or ``error`` is for.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

from .bridge_link import UpstreamLink
from .bridge_wire import rewrite_from_remote
from .logging import get_logger
from .machines import MachineRecord
from .protocol import MachineStatus, MachineSummary, scrub_wire_json

log = get_logger("tstd.bridge")

_LIST_SESSIONS = json.dumps({"type": "list_sessions"})


@dataclass(frozen=True)
class Timings:
    """The hub's clocks. Tests shrink them; production uses the defaults."""

    refresh_seconds: float = 30.0  # control link asks for rows this often
    linger_seconds: float = 60.0  # links stay up this long after the last viewer
    lane_idle_seconds: float = 300.0  # a lane with nothing attached closes
    queue_seconds: float = 10.0  # a verb waits this long for its lane
    queue_limit: int = 64  # and at most this many wait
    debounce_seconds: float = 1.0  # row refresh after a row event


DEFAULT_TIMINGS = Timings()


@dataclass
class Route:
    """One thing sent on a control link that expects an answer.

    ``viewer`` None is a ``list_sessions`` marker; otherwise an approval
    sent for that viewer, answered by nothing or by one ``error``.
    """

    viewer: Any
    session_id: str | None = None


class MachineHost(Protocol):
    """What a computer's state needs from the bridge that owns it."""

    timings: Timings

    def new_link(
        self,
        machine: MachineState,
        role: str,
        on_open: Callable[[], Awaitable[None]],
        on_frame: Callable[[dict[str, Any]], Awaitable[None]],
        on_status: Callable[[], None] | None,
    ) -> UpstreamLink: ...

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None: ...

    async def deliver(self, viewer: Any, payload: str) -> None: ...

    def set_rows(
        self, machine: MachineState, rows: list[dict[str, Any]], requester: Any
    ) -> None: ...

    def machines_changed(self) -> None: ...

    def wake_lanes(self, mch: str) -> None: ...


class MachineState:
    """One paired computer: record, control link, cached rows, status."""

    def __init__(self, record: MachineRecord, host: MachineHost) -> None:
        self.record = record
        self._host = host
        self.link: UpstreamLink | None = None
        self.rows: list[dict[str, Any]] = []
        self.status: MachineStatus = "connecting"
        self.detail: str | None = None
        self.routes: deque[Route] = deque()
        self._refresh: asyncio.TimerHandle | None = None

    @property
    def mch(self) -> str:
        return self.record.id

    @property
    def name(self) -> str:
        return self.record.name

    def current_status(self) -> tuple[MachineStatus, str | None]:
        if self.link is not None:
            return self.link.status, self.link.detail
        return self.status, self.detail

    def summary(self, *, own_machine_id: str | None, include_address: bool) -> MachineSummary:
        record = self.record
        status, detail = self.current_status()
        return MachineSummary(
            id=record.id,
            name=record.name,
            host=record.host if include_address else None,
            port=record.port if include_address else None,
            status=status,
            last_seen=record.last_seen,
            paired_at=record.paired_at,
            same_computer=own_machine_id is not None and record.machine_id == own_machine_id,
            detail=detail,
        )

    # ── the control link ──────────────────────────────────────────────

    def start_control(self) -> None:
        if self.link is not None and self.link.running:
            return
        self.link = self._host.new_link(
            self, "control", self._on_open, self._on_frame, self._host.machines_changed
        )
        self.link.start()

    async def stop_control(self) -> None:
        link, self.link = self.link, None
        if self._refresh is not None:
            self._refresh.cancel()
            self._refresh = None
        self.routes.clear()
        if link is not None:
            self.status, self.detail = link.status, link.detail
            if self.status == "connected":
                self.status = "connecting"
            await link.stop()

    def reset_backoff(self) -> None:
        """ "Try now": dial again, parked or not."""
        if self.link is not None:
            self.link.reset_backoff()

    def nudge(self) -> None:
        """A viewer arrived: dial again unless refused, locked or outdated."""
        if self.link is not None:
            self.link.nudge()

    def refresh(self, *, force: bool = False) -> None:
        """Ask for the computer's own rows (one request in flight at a time)."""
        link = self.link
        if link is None or not link.is_open:
            return
        if not force and any(route.viewer is None for route in self.routes):
            return
        if link.send(_LIST_SESSIONS):
            self.routes.append(Route(None))

    def debounce_refresh(self) -> None:
        if self._refresh is not None:
            return

        def fire() -> None:
            self._refresh = None
            self.refresh()

        self._refresh = asyncio.get_running_loop().call_later(
            self._host.timings.debounce_seconds, fire
        )

    def send_approval(self, viewer: Any, session_id: str, upstream: dict[str, Any]) -> bool:
        """An approval over the control link (no lane open). False when down."""
        link = self.link
        if link is None or not link.is_open or not link.send(json.dumps(upstream)):
            return False
        self.routes.append(Route(viewer, session_id))
        self.refresh(force=True)
        return True

    async def _on_open(self) -> None:
        self.routes.clear()
        self.refresh(force=True)
        self._host.wake_lanes(self.mch)

    async def _on_frame(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "session_list":
            # Answers run in send order on one socket: pop through our marker.
            while self.routes:
                if self.routes.popleft().viewer is None:
                    break
            out = rewrite_from_remote(frame, self.mch, self.name)
            if out is not None:
                self._host.set_rows(self, out.get("sessions") or [], None)
            return
        if kind != "error":
            return
        head = self.routes.popleft() if self.routes else None
        if head is None or head.viewer is None:
            log.info(
                "a paired computer refused a refresh",
                extra={"extra_fields": {"machine": self.mch, "code": frame.get("code")}},
            )
            return
        out = rewrite_from_remote(frame, self.mch, self.name)
        if out is None:
            return
        if out.get("session_id") is None and head.session_id is not None:
            out["session_id"] = head.session_id
        await self._host.deliver(head.viewer, scrub_wire_json(json.dumps(out)))
