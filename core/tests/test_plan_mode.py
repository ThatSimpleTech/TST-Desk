"""Tests for the set_plan_mode message and the plan lock (TD-4603)."""

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


async def _open_and_attach(ws: Any, path: str) -> str:
    """Open a workspace, attach past the open-time events, return the id.

    Open replays session_state, boundary_update, tier_state (seqs 1-3);
    attaching from 4 streams everything after that live.
    """
    await ws.send(json.dumps({"type": "open_workspace", "path": path}))
    session_state = dict(json.loads(await ws.recv()))
    session_id = session_state["session_id"]
    await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 4}))
    await asyncio.sleep(0.1)
    return session_id


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


class TestPlanMode:
    @pytest.mark.asyncio
    async def test_enable_acks_with_locked_tier_state(self, tmp_path: Path) -> None:
        """set_plan_mode acks with tier_state carrying plan_lock=True."""
        with tempfile.TemporaryDirectory() as tmp:
            daemon, daemon_task = await _start_daemon(tmp)
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)
            session_id = await _open_and_attach(ws, str(tmp_path))

            await ws.send(
                json.dumps({"type": "set_plan_mode", "session_id": session_id, "enabled": True})
            )
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert evt["type"] == "tier_state"
            assert evt["plan_lock"] is True
            assert evt["tier"] == "brain"

            sess = daemon.session_registry.get(session_id)
            assert sess is not None and sess.router is not None
            assert sess.router.plan_lock is True
            assert sess.router.active_tier == "brain"

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_set_tier_to_worker_refused_while_locked(self, tmp_path: Path) -> None:
        """The lock refuses set_tier to worker with a typed error."""
        with tempfile.TemporaryDirectory() as tmp:
            daemon, daemon_task = await _start_daemon(tmp)
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)
            session_id = await _open_and_attach(ws, str(tmp_path))

            await ws.send(
                json.dumps({"type": "set_plan_mode", "session_id": session_id, "enabled": True})
            )
            await asyncio.wait_for(ws.recv(), timeout=2)  # tier_state ack

            await ws.send(
                json.dumps({"type": "set_tier", "session_id": session_id, "tier": "worker"})
            )
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert response["type"] == "error"
            assert response["code"] == "bad_request"
            assert "plan lock" in response["message"]

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_set_tier_to_brain_allowed_while_locked(self, tmp_path: Path) -> None:
        """Pinning brain stays allowed under the lock."""
        with tempfile.TemporaryDirectory() as tmp:
            daemon, daemon_task = await _start_daemon(tmp)
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)
            session_id = await _open_and_attach(ws, str(tmp_path))

            await ws.send(
                json.dumps({"type": "set_plan_mode", "session_id": session_id, "enabled": True})
            )
            await asyncio.wait_for(ws.recv(), timeout=2)  # tier_state ack

            await ws.send(
                json.dumps({"type": "set_tier", "session_id": session_id, "tier": "brain"})
            )
            # set_tier logs a tier_switched timeline entry, then acks.
            switched = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert switched["type"] == "tier_switched"
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert response["type"] == "tier_state"
            assert response["override"] == "brain"
            assert response["plan_lock"] is True

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_clear_releases_the_override(self, tmp_path: Path) -> None:
        """Clearing the lock lets a pinned worker override route again."""
        with tempfile.TemporaryDirectory() as tmp:
            daemon, daemon_task = await _start_daemon(tmp)
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)
            session_id = await _open_and_attach(ws, str(tmp_path))

            # Pin worker first (fresh session: lead turns would say brain,
            # so the pin is observable the moment the lock clears). set_tier
            # logs a tier_switched entry before its tier_state ack.
            await ws.send(
                json.dumps({"type": "set_tier", "session_id": session_id, "tier": "worker"})
            )
            switched = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert switched["type"] == "tier_switched"
            pinned = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert pinned["override"] == "worker"

            await ws.send(
                json.dumps({"type": "set_plan_mode", "session_id": session_id, "enabled": True})
            )
            locked = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert locked["tier"] == "brain"
            assert locked["plan_lock"] is True

            await ws.send(
                json.dumps({"type": "set_plan_mode", "session_id": session_id, "enabled": False})
            )
            cleared = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert cleared["plan_lock"] is False
            assert cleared["tier"] == "worker"

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_plan_mode_unknown_session_returns_error(self) -> None:
        """set_plan_mode against an unknown session returns a typed error."""
        with tempfile.TemporaryDirectory() as tmp:
            daemon, daemon_task = await _start_daemon(tmp)
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)

            await ws.send(
                json.dumps({"type": "set_plan_mode", "session_id": "nonexistent", "enabled": True})
            )
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert response["type"] == "error"
            assert response["code"] == "session_not_found"

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)
