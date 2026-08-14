"""Tests for the set_tier message and tier_switched event (TD-1005)."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tstd.daemon import Daemon
from tstd.protocol import PROTOCOL_VERSION


async def _connect_and_handshake(uri: str, token: str) -> Any:
    """Open a connection and perform the hello handshake."""
    ws = await connect(uri)
    await ws.send(
        json.dumps(
            {
                "type": "hello",
                "token": token,
                "version": PROTOCOL_VERSION,
            }
        )
    )
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _open_workspace(ws: Any, path: str) -> dict[str, Any]:
    """Send open_workspace and return the session_state response."""
    await ws.send(json.dumps({"type": "open_workspace", "path": path}))
    return dict(json.loads(await ws.recv()))


async def _start_daemon(tmp: str) -> tuple[Daemon, asyncio.Task[Any]]:
    """Start a daemon on a temp dir and wait for its port."""
    daemon = Daemon(data_dir=Path(tmp))
    task = asyncio.create_task(daemon.run())
    for _ in range(50):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


class TestSetTier:
    @pytest.mark.asyncio
    async def test_set_tier_emits_tier_switched_event(self) -> None:
        """set_tier applies the override and logs a tier_switched event."""
        with tempfile.TemporaryDirectory() as tmp:
            daemon, daemon_task = await _start_daemon(tmp)
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)

            session_state = await _open_workspace(ws, "/tmp/test")
            session_id = session_state["session_id"]

            # A fresh session routes its lead turns through brain, so the
            # switch's `previous` should be brain.
            await ws.send(
                json.dumps({"type": "set_tier", "session_id": session_id, "tier": "worker"})
            )
            await asyncio.sleep(0.1)

            # The override reached the session's live router.
            sess = daemon.session_registry.get(session_id)
            assert sess is not None
            router = sess.router
            assert router is not None
            assert router.has_override is True
            assert router.active_tier == "worker"

            # Replay from seq 1: session_state, boundary_update, tier_state
            # (open-time snapshot, TD-1006), tier_switched, tier_state (ack).
            await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 1}))
            replayed = []
            for _ in range(5):
                evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                replayed.append(evt)

            assert [e["type"] for e in replayed] == [
                "session_state",
                "boundary_update",
                "tier_state",
                "tier_switched",
                "tier_state",
            ]
            switched = replayed[3]
            assert switched["tier"] == "worker"
            assert switched["previous"] == "brain"
            assert switched["seq"] == 4
            ack = replayed[4]
            assert ack["tier"] == "worker"
            assert ack["override"] == "worker"

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_set_tier_streams_to_attached_client(self) -> None:
        """A tier_switched event reaches an already-attached client live."""
        with tempfile.TemporaryDirectory() as tmp:
            daemon, daemon_task = await _start_daemon(tmp)
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)

            session_state = await _open_workspace(ws, "/tmp/test")
            session_id = session_state["session_id"]

            # Attach (from seq 4 to skip the open-time events: session_state,
            # boundary_update, tier_state).
            await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 4}))
            await asyncio.sleep(0.1)

            await ws.send(
                json.dumps({"type": "set_tier", "session_id": session_id, "tier": "brain"})
            )

            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert evt["type"] == "tier_switched"
            assert evt["tier"] == "brain"
            assert evt["previous"] == "brain"
            assert evt["seq"] == 4

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_set_tier_unknown_session_returns_error(self) -> None:
        """set_tier against an unknown session returns a typed error."""
        with tempfile.TemporaryDirectory() as tmp:
            daemon, daemon_task = await _start_daemon(tmp)
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)

            await ws.send(
                json.dumps({"type": "set_tier", "session_id": "nonexistent", "tier": "worker"})
            )
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert response["type"] == "error"
            assert response["code"] == "session_not_found"

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)
