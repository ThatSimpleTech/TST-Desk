"""Tests for the new_session verb (TD-1701).

``new_session`` anchors on an existing session: the daemon creates, wires,
and starts a fresh session in the anchor's workspace — the same creation
path as ``open_workspace``, minus path validation and config scaffolding,
because the path comes from the registry rather than the client.
"""

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
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _open_workspace(ws: Any, path: str) -> dict[str, Any]:
    """Send open_workspace and return the session_state response."""
    await ws.send(json.dumps({"type": "open_workspace", "path": path}))
    return dict(json.loads(await ws.recv()))


class _RunningDaemon:
    """Spin a daemon up on a throwaway data dir and tear it down."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.daemon = Daemon(data_dir=Path(self._tmp.name))

    async def __aenter__(self) -> Daemon:
        self._task = asyncio.create_task(self.daemon.run())
        for _ in range(50):
            if self.daemon.ws_server.port:
                break
            await asyncio.sleep(0.05)
        assert self.daemon.ws_server.port > 0
        return self.daemon

    async def __aexit__(self, *_exc: Any) -> None:
        self.daemon._shutdown_event.set()
        await asyncio.gather(self._task, return_exceptions=True)
        self._tmp.cleanup()


class TestNewSession:
    @pytest.mark.asyncio
    async def test_creates_distinct_session_in_anchor_workspace(self, tmp_path: Path) -> None:
        """new_session replies with a fresh session in the anchor's workspace."""
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            opened = await _open_workspace(ws, str(tmp_path))
            anchor_id = opened["session_id"]

            await ws.send(json.dumps({"type": "new_session", "session_id": anchor_id}))
            reply = dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))

            assert reply["type"] == "session_state"
            assert reply["state"] == "running"
            assert reply["seq"] == 1
            new_id = reply["session_id"]
            assert new_id != anchor_id

            sess = daemon.session_registry.get(new_id)
            assert sess is not None
            assert sess.workspace_path == str(tmp_path)
            await ws.close()

    @pytest.mark.asyncio
    async def test_new_session_is_wired_like_open(self, tmp_path: Path) -> None:
        """The fresh session gets a runner and the opening boundary/tier events."""
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            opened = await _open_workspace(ws, str(tmp_path))
            await ws.send(json.dumps({"type": "new_session", "session_id": opened["session_id"]}))
            reply = dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))
            new_id = reply["session_id"]

            # Live loop registered — this is not a tombstone.
            assert daemon.session_registry.get_runner(new_id) is not None

            # Same opening event shape as open_workspace (TD-706/TD-1006).
            sess = daemon.session_registry.get(new_id)
            assert sess is not None
            types = [e.type for e in sess.event_log.events_from(1)]
            assert types == ["session_state", "boundary_update", "tier_state"]

            # Both sessions are listed, sorted or not — the client orders.
            store_ids = {r.session_id for r in daemon._session_store.records()}
            assert store_ids == {opened["session_id"], new_id}
            await ws.close()

    @pytest.mark.asyncio
    async def test_unknown_anchor_is_typed_error(self, tmp_path: Path) -> None:
        """Anchoring on a session the daemon doesn't know returns session_not_found."""
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            await _open_workspace(ws, str(tmp_path))
            await ws.send(json.dumps({"type": "new_session", "session_id": "no-such-id"}))
            reply = dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))
            assert reply["type"] == "error"
            assert reply["code"] == "session_not_found"
            await ws.close()
