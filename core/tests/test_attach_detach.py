"""Tests for attach, detach, and replay (TD-206)."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tstd.daemon import Daemon
from tstd.protocol import PROTOCOL_VERSION, AssistantDelta


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
    resp = await ws.recv()
    return dict(json.loads(resp))


class TestAttachDetachIntegration:
    @pytest.mark.asyncio
    async def test_attach_replays_events_from_seq(self, tmp_path: Path) -> None:
        """attach with from_seq replays events starting at that seq."""
        with tempfile.TemporaryDirectory() as tmp:
            daemon = Daemon(data_dir=Path(tmp))

            daemon_task = asyncio.create_task(daemon.run())
            for _ in range(50):
                if daemon.ws_server.port:
                    break
                await asyncio.sleep(0.05)
            assert daemon.ws_server.port > 0

            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)

            # Open a workspace — creates a session
            session_state = await _open_workspace(ws, str(tmp_path))
            assert session_state["type"] == "session_state"
            session_id = session_state["session_id"]

            # Emit events by adding them to the session directly
            session = daemon.session_registry.get(session_id)
            assert session is not None
            await session.event_log.add(
                AssistantDelta(session_id=session_id, delta="hello ", seq=1)
            )
            await session.event_log.add(AssistantDelta(session_id=session_id, delta="world", seq=1))

            # Attach from seq 1 — should replay everything (session_state,
            # boundary_update, tier_state (TD-1006) at open, then the two
            # deltas).
            await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 1}))
            replayed = []
            for _ in range(5):
                evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                replayed.append(evt)

            types = [e["type"] for e in replayed]
            assert types == [
                "session_state",
                "boundary_update",
                "tier_state",
                "assistant_delta",
                "assistant_delta",
            ]
            assert [e["seq"] for e in replayed] == [1, 2, 3, 4, 5]

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_attach_streams_live_events(self, tmp_path: Path) -> None:
        """After replay, new events stream live to the attached client."""
        with tempfile.TemporaryDirectory() as tmp:
            daemon = Daemon(data_dir=Path(tmp))
            daemon_task = asyncio.create_task(daemon.run())
            for _ in range(50):
                if daemon.ws_server.port:
                    break
                await asyncio.sleep(0.05)
            assert daemon.ws_server.port > 0

            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)
            session_state = await _open_workspace(ws, str(tmp_path))
            session_id = session_state["session_id"]

            session = daemon.session_registry.get(session_id)
            assert session is not None

            # Attach from seq 4 (skip session_state + boundary_update +
            # tier_state, all emitted at open (TD-706/1006)).
            await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 4}))

            # Emit a new event after attach
            await session.event_log.add(AssistantDelta(session_id=session_id, delta="live!", seq=1))

            # The attached client should receive it live
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert evt["type"] == "assistant_delta"
            assert evt["delta"] == "live!"
            assert evt["seq"] == 4

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_no_gaps_no_duplicates_under_concurrent_write(self, tmp_path: Path) -> None:
        """Replay and live stream produce no gaps and no duplicates
        while events are being written concurrently."""
        with tempfile.TemporaryDirectory() as tmp:
            daemon = Daemon(data_dir=Path(tmp))
            daemon_task = asyncio.create_task(daemon.run())
            for _ in range(50):
                if daemon.ws_server.port:
                    break
                await asyncio.sleep(0.05)
            assert daemon.ws_server.port > 0

            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)
            session_state = await _open_workspace(ws, str(tmp_path))
            session_id = session_state["session_id"]
            session = daemon.session_registry.get(session_id)
            assert session is not None

            # Start concurrent writers
            async def write_events() -> None:
                for i in range(20):
                    await session.event_log.add(
                        AssistantDelta(session_id=session_id, delta=f"w{i}", seq=1)
                    )
                    await asyncio.sleep(0.005)

            writer = asyncio.create_task(write_events())

            # Attach from seq 1 (replay everything) while writes are ongoing
            await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 1}))

            # Collect all events: open triple (session_state + boundary_update
            # + tier_state) + 20 deltas
            expected_total = 23
            received: list[int] = []
            while len(received) < expected_total:
                evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                received.append(evt["seq"])

            # Wait for the writer to finish
            await asyncio.gather(writer, return_exceptions=True)

            expected = list(range(1, expected_total + 1))
            assert received == expected, (
                f"Gaps or duplicates: received {received}, expected {expected}"
            )

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_detach_stops_streaming(self, tmp_path: Path) -> None:
        """detach stops the live stream without affecting the session."""
        with tempfile.TemporaryDirectory() as tmp:
            daemon = Daemon(data_dir=Path(tmp))
            daemon_task = asyncio.create_task(daemon.run())
            for _ in range(50):
                if daemon.ws_server.port:
                    break
                await asyncio.sleep(0.05)
            assert daemon.ws_server.port > 0

            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)
            session_state = await _open_workspace(ws, str(tmp_path))
            session_id = session_state["session_id"]
            session = daemon.session_registry.get(session_id)
            assert session is not None

            # Attach (from seq 4 — skip session_state + boundary_update +
            # tier_state from open)
            await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 4}))
            await asyncio.sleep(0.1)

            # Detach
            await ws.send(json.dumps({"type": "detach", "session_id": session_id}))
            await asyncio.sleep(0.1)

            # Emit an event after detach
            await session.event_log.add(
                AssistantDelta(session_id=session_id, delta="after-detach", seq=1)
            )

            # The client should NOT receive it — verify with a short timeout
            with pytest.raises((asyncio.TimeoutError, TimeoutError)):
                await asyncio.wait_for(ws.recv(), timeout=0.3)

            # But the session is unaffected
            assert session.state == "running"
            assert session.event_log.last_seq == 4

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_attach_unknown_session_returns_error(self) -> None:
        """Attaching to an unknown session returns a typed error."""
        with tempfile.TemporaryDirectory() as tmp:
            daemon = Daemon(data_dir=Path(tmp))
            daemon_task = asyncio.create_task(daemon.run())
            for _ in range(50):
                if daemon.ws_server.port:
                    break
                await asyncio.sleep(0.05)
            assert daemon.ws_server.port > 0

            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)

            await ws.send(
                json.dumps({"type": "attach", "session_id": "nonexistent", "from_seq": 1})
            )
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert response["type"] == "error"
            assert response["code"] == "session_not_found"

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_two_clients_receive_all_events(self, tmp_path: Path) -> None:
        """Two clients attached to one session both receive all events."""
        with tempfile.TemporaryDirectory() as tmp:
            daemon = Daemon(data_dir=Path(tmp))
            daemon_task = asyncio.create_task(daemon.run())
            for _ in range(50):
                if daemon.ws_server.port:
                    break
                await asyncio.sleep(0.05)
            assert daemon.ws_server.port > 0

            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws1 = await _connect_and_handshake(uri, daemon.ws_server.token)
            ws2 = await _connect_and_handshake(uri, daemon.ws_server.token)

            # Client 1 opens the workspace
            session_state = await _open_workspace(ws1, str(tmp_path))
            session_id = session_state["session_id"]
            session = daemon.session_registry.get(session_id)
            assert session is not None

            # Both attach (from seq 4 to skip the three open events)
            await ws1.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 4}))
            await ws2.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 4}))
            await asyncio.sleep(0.1)

            # Emit two events
            await session.event_log.add(AssistantDelta(session_id=session_id, delta="one", seq=1))
            await session.event_log.add(AssistantDelta(session_id=session_id, delta="two", seq=1))

            # Both clients should receive both events
            for ws in (ws1, ws2):
                evt1 = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                evt2 = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert evt1["type"] == "assistant_delta"
                assert evt2["type"] == "assistant_delta"
                assert [evt1["seq"], evt2["seq"]] == [4, 5]

            await ws1.close()
            await ws2.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)
