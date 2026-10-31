"""The hub: other computers' sessions through this daemon (TD-5015).

While a viewer is connected, each paired computer has a **control link**
(presence, last-seen, cached rows; see ``bridge_machine``), stopped 60 s
after the last viewer leaves. A viewer's first verb for a bridged id
(``<raw>:<mch>``) opens a **lane**, that viewer's own 1:1 socket to that
computer (``bridge_lane``), so attach, replay and seq keep the meaning
the other computer gives one connection.

Nothing here blocks the viewer's socket loop: :meth:`Bridge.forward`
only enqueues, cached rows answer ``list_sessions``, and pairing runs as
a task. Approvals never wait: the open lane, else the open control link,
else ``machine_unreachable`` at once. This module never dials; the links
and ``machine_pairing`` do, only to paired Tailscale hosts.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from .bridge_lane import Lane
from .bridge_link import PARKED_STATUSES, Sleep, UpstreamLink
from .bridge_machine import DEFAULT_TIMINGS, MachineState, Timings
from .bridge_pair import MachineKeys, forget_machine, pair_machine
from .bridge_wire import rewrite_to_remote
from .logging import get_logger
from .machine_pairing import DEFAULT_POLICY, DialPolicy, PairError, PairTarget, parse_target
from .machines import MachineRecord, MachineRegistry
from .peer_policy import APPROVAL_VERBS
from .protocol import MachineList, MachineStatus, MachineSummary, build_error

log = get_logger("tstd.bridge")

Deliver = Callable[[Any, str], Awaitable[None]]
OnRows = Callable[[bool, Any], Coroutine[Any, Any, None]]
OnMachines = Callable[[], Coroutine[Any, Any, None]]


class Bridge:
    """Paired computers, their control links, rows and lanes.

    ``on_rows(changed, requester)`` runs when a computer's rows changed
    (push to every viewer) or a viewer asked (answer that one).
    ``on_machines()`` runs when the list or a status changed.
    """

    def __init__(
        self,
        registry: MachineRegistry,
        *,
        machine_id: Callable[[], str | None],
        deliver: Deliver,
        on_rows: OnRows,
        on_machines: OnMachines,
        policy: DialPolicy = DEFAULT_POLICY,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
        link_sleep: Sleep = asyncio.sleep,
        timings: Timings = DEFAULT_TIMINGS,
        wants_history: Callable[[Any], bool] | None = None,
    ) -> None:
        self.registry = registry
        self._machine_id = machine_id
        self._deliver = deliver
        self._wants_history = wants_history
        self._on_rows = on_rows
        self._on_machines = on_machines
        self.policy = policy
        self.clock = clock
        self._wall = wall
        self._jitter = jitter
        self._link_sleep = link_sleep
        self.timings = timings
        self.keys = MachineKeys()
        self._machines: dict[str, MachineState] = {
            r.id: MachineState(r, self) for r in registry.records()
        }
        self._lanes: dict[tuple[Any, str], Lane] = {}
        self._viewers: set[Any] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._tick: asyncio.Task[None] | None = None
        self._linger: asyncio.Task[None] | None = None
        self._adding = False
        self._stopped = False

    # ── plumbing (also what lanes and computers call back) ────────────

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        if self._stopped:
            coro.close()
            return
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def deliver(self, viewer: Any, payload: str) -> None:
        with contextlib.suppress(Exception):
            await self._deliver(viewer, payload)

    def wants_history(self, viewer: Any) -> bool:
        """Whether *viewer*'s hello asked for ``conversation_history`` (TD-5201)."""
        return self._wants_history is not None and self._wants_history(viewer)

    def unreachable(
        self,
        machine: MachineState,
        session_id: str,
        detail: str | None = None,
        *,
        in_reply_to: str | None = None,
    ) -> str:
        """``machine_unreachable`` for one verb; ``in_reply_to`` names it (TD-5144)."""
        return build_error(
            "machine_unreachable",
            detail or f"{machine.name} isn't reachable right now. Try again when it is.",
            session_id=session_id,
            machine=machine.mch,
            in_reply_to=in_reply_to,
        )

    def machines_changed(self) -> None:
        self.spawn(self._on_machines())

    def wake_lanes(self, mch: str) -> None:
        for key, lane in list(self._lanes.items()):
            if key[1] == mch:
                lane.wake()

    def new_link(
        self,
        machine: MachineState,
        role: str,
        on_open: Callable[[], Awaitable[None]],
        on_frame: Callable[[dict[str, Any]], Awaitable[None]],
        on_status: Callable[[], None] | None,
    ) -> UpstreamLink:
        mch = machine.mch
        record = machine.record

        async def key() -> str:
            return await self.keys.get(mch)

        def seen(gone: bool) -> None:
            if role == "control" and self.registry.touch_last_seen(mch, force=gone):
                fresh = self.registry.get(mch)
                if fresh is not None:
                    machine.record = fresh

        return UpstreamLink(
            label=f"{mch}/{role}",
            host=record.host,
            port=record.port,
            key=key,
            policy=self.policy,
            on_open=on_open,
            on_frame=on_frame,
            on_status=on_status,
            on_seen=seen,
            name=record.name,
            clock=self.clock,
            jitter=self._jitter,
            sleep=self._link_sleep,
        )

    def set_rows(self, machine: MachineState, rows: list[dict[str, Any]], requester: Any) -> None:
        """New rows for one computer; viewers hear only when they changed."""
        changed = rows != machine.rows
        machine.rows = rows
        if changed or requester is not None:
            self.spawn(self._on_rows(changed, requester))

    # ── viewers ───────────────────────────────────────────────────────

    def has_viewers(self) -> bool:
        return bool(self._viewers)

    def viewer_connected(self, viewer: Any) -> None:
        """A viewer finished its handshake: links up, backoff reset, rows fresh.

        A parked computer (refused, locked, outdated) stays parked: only
        "Try now" or its own 5-minute retry dials it again (A.7).
        """
        if self._stopped:
            return
        self._viewers.add(viewer)
        if self._linger is not None:
            self._linger.cancel()
            self._linger = None
        for machine in self._machines.values():
            machine.start_control()
            machine.nudge()
            machine.refresh()
        if self._tick is None or self._tick.done():
            self._tick = asyncio.create_task(self._tick_loop(), name="bridge-tick")

    async def viewer_closed(self, viewer: Any) -> None:
        """Close that viewer's lanes; stop dialing 60 s after the last one leaves."""
        if viewer not in self._viewers:
            return
        self._viewers.discard(viewer)
        for key in [key for key in self._lanes if key[0] is viewer]:
            await self._lanes.pop(key).stop()
        if not self._viewers and not self._stopped:
            self._linger = asyncio.create_task(self._linger_then_stop(), name="bridge-linger")

    async def _linger_then_stop(self) -> None:
        await asyncio.sleep(self.timings.linger_seconds)
        if self._viewers:
            return
        if self._tick is not None:
            self._tick.cancel()
            self._tick = None
        for machine in self._machines.values():
            await machine.stop_control()
        log.info("stopped dialing paired computers: no window is open")

    async def _tick_loop(self) -> None:
        while self._viewers:
            await asyncio.sleep(self.timings.refresh_seconds)
            for machine in self._machines.values():
                machine.refresh()
            now = self.clock()
            for key, lane in list(self._lanes.items()):
                if lane.idle(now):
                    self._lanes.pop(key, None)
                    await lane.stop()

    # ── what viewers see ──────────────────────────────────────────────

    def cached_rows(self) -> list[dict[str, Any]]:
        """Every computer's last-known rows, unreachable ones included."""
        rows: list[dict[str, Any]] = []
        for machine in self._machines.values():
            rows.extend(dict(row) for row in machine.rows)
        return rows

    def summaries(self, *, include_address: bool) -> list[MachineSummary]:
        own = self._machine_id()
        return [
            m.summary(own_machine_id=own, include_address=include_address)
            for m in self._machines.values()
        ]

    def machine_list(self, *, include_address: bool) -> str:
        machines = self.summaries(include_address=include_address)
        return MachineList(machines=machines).model_dump_json()

    def known(self, mch: str) -> bool:
        return mch in self._machines

    def status_of(self, mch: str) -> MachineStatus | None:
        machine = self._machines.get(mch)
        return None if machine is None else machine.current_status()[0]

    def lane(self, viewer: Any, mch: str) -> Lane | None:
        return self._lanes.get((viewer, mch))

    # ── forwarding ────────────────────────────────────────────────────

    def _ensure_lane(self, viewer: Any, machine: MachineState, *, dial: bool = True) -> Lane:
        key = (viewer, machine.mch)
        lane = self._lanes.get(key)
        if lane is None:
            lane = Lane(self, viewer, machine, dial=dial)
            self._lanes[key] = lane
        elif dial and lane.dormant:
            lane.wake()  # made while its computer was parked
        return lane

    def forward(self, viewer: Any, frame: dict[str, Any]) -> str | None:
        """Send one viewer verb for a bridged id. Never awaits the network.

        Returns an error frame to answer at once, or None (any answer comes
        down the lane). Every refusal names the verb it answers (TD-5144).
        """
        kind = frame.get("type")
        verb = kind if isinstance(kind, str) else None
        parts = rewrite_to_remote(frame)
        sid = frame.get("session_id")
        if parts is None or not isinstance(sid, str):
            return build_error(
                "bad_request", "That isn't another computer's session.", in_reply_to=verb
            )
        mch, upstream = parts
        machine = self._machines.get(mch)
        if machine is None:
            return build_error(
                "machine_not_found",
                "That computer isn't paired with this one anymore.",
                session_id=sid,
                machine=mch,
                in_reply_to=verb,
            )
        status, detail = machine.current_status()
        parked = status in PARKED_STATUSES
        if parked and kind not in ("attach", "detach"):
            # Refused, locked or outdated: answer now, never dial for it.
            return self.unreachable(machine, sid, detail, in_reply_to=verb)
        lane = self._lanes.get((viewer, mch))
        if kind in APPROVAL_VERBS:
            # Never queued: the open lane, else the open control link.
            if lane is not None and lane.is_open:
                lane.send(upstream)
                return None
            self._ensure_lane(viewer, machine)
            if machine.send_approval(viewer, sid, upstream):
                return None
            return self.unreachable(machine, sid, in_reply_to=verb)
        # A parked computer's lane only remembers attaches until it is back.
        lane = self._ensure_lane(viewer, machine, dial=not parked)
        raw = upstream["session_id"]
        if kind == "attach":
            lane.attach(raw, upstream)
            return None
        if kind == "detach":
            lane.detach(raw, upstream)
            return None
        if lane.is_open:
            lane.send(upstream)
            return None
        if status not in ("connecting", "connected"):
            return self.unreachable(machine, sid, in_reply_to=verb)
        return lane.enqueue(upstream, sid)

    # ── machine verbs ─────────────────────────────────────────────────

    def start_add(
        self,
        requester: Any,
        *,
        link: str | None,
        host: str | None,
        port: int | None,
        code: str | None,
    ) -> str | None:
        """Validate, then pair in the background. An error frame, or None."""
        if self._adding:
            return build_error("machine_busy", "Already pairing a computer. Wait for it to finish.")
        try:
            target = parse_target(link=link, host=host, port=port, code=code, policy=self.policy)
        except PairError as e:
            return build_error(e.code, e.message)
        self._adding = True
        self.spawn(self._add(requester, target))
        return None

    async def _add(self, requester: Any, target: PairTarget) -> None:
        try:
            record = await pair_machine(
                self.registry,
                self.keys,
                target,
                policy=self.policy,
                wall=self._wall,
                spawn=self.spawn,
            )
            await self._install(record)
        except PairError as e:
            await self.deliver(requester, build_error(e.code, e.message))
            return
        except Exception:
            log.exception("pairing a computer failed")
            await self.deliver(requester, build_error("pair_failed", "Pairing didn't work."))
            return
        finally:
            self._adding = False
        await self._on_machines()
        if requester not in self._viewers:
            await self.deliver(requester, self.machine_list(include_address=True))

    async def _install(self, record: MachineRecord) -> None:
        machine = self._machines.get(record.id)
        if machine is None:
            machine = MachineState(record, self)
            self._machines[record.id] = machine
        else:
            # Re-paired: same mch, new address or key. Open lanes keep what
            # their viewer attached and swap only their socket, so bridged
            # panes keep streaming from where they were.
            await machine.stop_control()
            machine.record = record
            machine.status, machine.detail = "connecting", None
            for key, lane in list(self._lanes.items()):
                if key[1] == record.id:
                    await lane.relink()
        if self._viewers:
            machine.start_control()

    async def remove(self, mch: str) -> str | None:
        """Forget one computer. An error frame, or None (answer: machine_list)."""
        machine = self._machines.get(mch)
        if machine is None:
            return build_error("machine_not_found", "That computer isn't paired with this one.")
        error = await forget_machine(
            self.registry, self.keys, machine.record, policy=self.policy, spawn=self.spawn
        )
        if error is not None:
            return error
        del self._machines[mch]
        lanes = [self._lanes.pop(k) for k in [k for k in self._lanes if k[1] == mch]]
        self.spawn(self._close_removed(machine, lanes))
        self.spawn(self._on_rows(bool(machine.rows), None))
        self.spawn(self._on_machines())
        return None

    async def _close_removed(self, machine: MachineState, lanes: list[Lane]) -> None:
        await machine.stop_control()
        for lane in lanes:
            await lane.stop()

    def reconnect(self, mch: str) -> str | None:
        """ "Try now": dial again without waiting out the backoff."""
        machine = self._machines.get(mch)
        if machine is None:
            return build_error("machine_not_found", "That computer isn't paired with this one.")
        if self._viewers:
            machine.start_control()
        machine.reset_backoff()
        self.wake_lanes(mch)
        return None

    # ── lifecycle ─────────────────────────────────────────────────────

    async def stop(self) -> None:
        self._stopped = True
        for task in (self._tick, self._linger):
            if task is not None:
                task.cancel()
        self._tick = self._linger = None
        lanes = list(self._lanes.values())
        self._lanes.clear()
        for lane in lanes:
            await lane.stop()
        for machine in self._machines.values():
            await machine.stop_control()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.keys.clear()
