"""The daemon's side of TD-5015: peer policy, machine verbs, merged lists.

The daemon keeps one :class:`Hub`. It owns ``computers.yaml`` and the
:class:`~tstd.bridge.Bridge`, and it sits at the daemon's two message
chokepoints:

- :meth:`Hub.intercept` runs after parsing and before dispatch. It
  refuses what a peer may not send (``peer_policy``), answers the four
  machine verbs, and hands a viewer's bridged-id verb to the bridge. It
  never awaits the network (pairing runs as a task).
- :meth:`Hub.finish` runs on every direct reply. A viewer's
  ``session_list`` gains every paired computer's cached rows, and a
  remote peer's ``setup_state`` loses ``remote_devices``,
  ``remote_bind`` and ``remote_port``.

This module never dials; the bridge's links do.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .bridge import Bridge
from .bridge_machine import DEFAULT_TIMINGS, Timings
from .bridge_wire import is_bridged
from .logging import get_logger
from .machine_pairing import DEFAULT_POLICY, DialPolicy
from .machines import MachineRegistry
from .peer_policy import MACHINE_VERBS, check, message_session_id
from .protocol import (
    AddMachine,
    ClientMessage,
    ListMachines,
    ReconnectMachine,
    RemoveMachine,
    scrub_wire_json,
)
from .ws import WebSocketServer

log = get_logger("tstd.hub")

# What a remote peer's setup_state leaves out (A.11): this computer's own
# paired devices and its Tailscale address are for its own window only.
REMOTE_SETUP_STATE_DROPS = ("remote_devices", "remote_bind", "remote_port")

_TYPE_RE = re.compile(r'^\{\s*"type"\s*:\s*"([a-z_]+)"')

# intercept() said nothing: the daemon dispatches the message itself.
NOT_HANDLED = object()


def frame_type(payload: str) -> str | None:
    """The ``type`` of a serialized frame whose first key is ``type``."""
    match = _TYPE_RE.match(payload)
    return match.group(1) if match else None


def scrub_setup_state(payload: str) -> str:
    data = json.loads(payload)
    for key in REMOTE_SETUP_STATE_DROPS:
        data.pop(key, None)
    return json.dumps(data, ensure_ascii=False)


def merge_session_list(payload: str, extra: list[dict[str, Any]]) -> str:
    """One ``session_list`` with *extra* rows after this computer's own."""
    if not extra:
        return payload
    data = json.loads(payload)
    own = data.get("sessions")
    data["sessions"] = (own if isinstance(own, list) else []) + extra
    return json.dumps(data, ensure_ascii=False)


class Hub:
    """Paired computers for one daemon."""

    def __init__(
        self,
        data_dir: Path,
        ws_server: WebSocketServer,
        own_session_list: Callable[[], Awaitable[str]],
        *,
        policy: DialPolicy | None = None,
        timings: Timings = DEFAULT_TIMINGS,
    ) -> None:
        self._ws = ws_server
        self._own_session_list = own_session_list
        self.registry = MachineRegistry(data_dir)
        self.registry.load()
        self.bridge = Bridge(
            self.registry,
            machine_id=lambda: ws_server.machine_id,
            deliver=self._send,
            on_rows=self._on_rows,
            on_machines=self._on_machines,
            policy=policy if policy is not None else DEFAULT_POLICY,
            timings=timings,
        )

    # ── connection lifecycle (ws callbacks) ───────────────────────────

    async def on_handshake(self, connection: Any) -> None:
        """A viewer gets the computer list and starts the control links."""
        peer = self._ws.peer_view(connection)
        if not peer.viewer:
            return
        self.bridge.viewer_connected(connection)
        await self._send(connection, self.bridge.machine_list(include_address=peer.loopback))

    async def on_closed(self, connection: Any) -> None:
        await self.bridge.viewer_closed(connection)

    async def stop(self) -> None:
        await self.bridge.stop()

    # ── the two chokepoints ───────────────────────────────────────────

    async def intercept(self, msg: ClientMessage, connection: Any) -> object:
        """A reply (str or None) when the hub handles *msg*, else NOT_HANDLED."""
        peer = self._ws.peer_view(connection)
        sid = message_session_id(msg)
        bridged = sid is not None and is_bridged(sid)
        refusal = check(msg, peer, bridged=bridged)
        if refusal is not None:
            return refusal
        if msg.type in MACHINE_VERBS:
            return await self._machine_verb(msg, connection, include_address=peer.loopback)
        if bridged and peer.viewer:
            return self.bridge.forward(connection, msg.model_dump(mode="json"))
        return NOT_HANDLED

    def finish(self, reply: str, connection: Any) -> str:
        """Merge a viewer's session_list; scrub a remote peer's setup_state."""
        kind = frame_type(reply)
        if kind not in ("session_list", "setup_state"):
            return reply
        peer = self._ws.peer_view(connection)
        if kind == "session_list" and peer.viewer:
            return merge_session_list(reply, self.bridge.cached_rows())
        if kind == "setup_state" and self._ws.peer_is_remote(connection):
            return scrub_setup_state(reply)
        return reply

    # ── machine verbs ─────────────────────────────────────────────────

    async def _machine_verb(
        self, msg: ClientMessage, connection: Any, *, include_address: bool
    ) -> str | None:
        if isinstance(msg, AddMachine):
            return self.bridge.start_add(
                connection, link=msg.link, host=msg.host, port=msg.port, code=msg.code
            )
        error: str | None = None
        if isinstance(msg, RemoveMachine):
            error = await self.bridge.remove(msg.machine)
        elif isinstance(msg, ReconnectMachine):
            error = self.bridge.reconnect(msg.machine)
        elif not isinstance(msg, ListMachines):
            return None
        return error or self.bridge.machine_list(include_address=include_address)

    # ── bridge callbacks ──────────────────────────────────────────────

    async def _send(self, connection: Any, payload: str) -> None:
        with contextlib.suppress(Exception):
            await connection.send(payload)

    async def _merged_list(self) -> str:
        own = await self._own_session_list()
        return scrub_wire_json(merge_session_list(own, self.bridge.cached_rows()))

    async def _on_rows(self, changed: bool, requester: Any) -> None:
        try:
            payload = await self._merged_list()
        except Exception:
            log.exception("could not build the merged session list")
            return
        targets = self._ws.viewer_connections() if changed else []
        if requester is not None and requester not in targets:
            targets.append(requester)
        for connection in targets:
            await self._send(connection, payload)

    async def _on_machines(self) -> None:
        for connection in self._ws.viewer_connections():
            peer = self._ws.peer_view(connection)
            await self._send(connection, self.bridge.machine_list(include_address=peer.loopback))
