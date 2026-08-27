"""Daemon half of resume healing (TD-1716).

The client cannot tell a suspended webview from a healthy quiet one without
help: the socket stays open and the OS answers transport ping/pong on the
frozen page's behalf.  The daemon's contribution is an application-level
``ping`` frame — one the client's own event loop has to process — plus a
re-attach that stays lossless however many times a resuming client issues it.
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
from tstd.protocol import (
    PROTOCOL_VERSION,
    AssistantDelta,
    DaemonEvent,
    Ping,
    build_ping,
    parse_daemon_event,
)
from tstd.ws import PING_INTERVAL_SECONDS, WebSocketServer


async def _connect_and_handshake(uri: str, token: str) -> Any:
    """Open a connection and perform the hello handshake."""
    ws = await connect(uri)
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _await_port(daemon: Daemon) -> None:
    for _ in range(50):
        if daemon.ws_server.port:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("daemon never bound a port")


class TestPingFrame:
    def test_ping_carries_no_session_and_no_seq(self) -> None:
        """The whole frame is its type — nothing to attribute, nothing to
        sequence."""
        assert json.loads(build_ping()) == {"type": "ping"}

    def test_ping_is_not_a_sequenced_daemon_event(self) -> None:
        """Deliberate: ``seq`` is the per-session log's contract and a ping
        belongs to no session's log."""
        assert not isinstance(Ping(), DaemonEvent)

    def test_ping_parses_as_a_daemon_frame(self) -> None:
        parsed = parse_daemon_event(build_ping())
        assert isinstance(parsed, Ping)

    def test_default_interval_leaves_room_for_a_missed_frame(self) -> None:
        """The client calls 30s of silence a zombie, so the cadence has to
        make one dropped ping survivable."""
        assert PING_INTERVAL_SECONDS * 2 <= 30

    def test_omitted_interval_follows_the_module_constant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default is looked up at init, so a test can quiet the cadence
        without restating every constructor."""
        monkeypatch.setattr("tstd.ws.PING_INTERVAL_SECONDS", 3.0)
        server = WebSocketServer(tmp_path)
        assert server._ping_interval == 3.0


class TestPingEmission:
    @pytest.mark.asyncio
    async def test_server_pings_a_handshaken_client_on_a_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp), ping_interval=0.05)
            await server.start()
            ws = await _connect_and_handshake(f"ws://127.0.0.1:{server.port}", server.token)

            for _ in range(2):
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert frame == {"type": "ping"}

            await ws.close()
            await server.stop()

    @pytest.mark.asyncio
    async def test_pings_stop_with_the_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp), ping_interval=0.05)
            await server.start()
            assert server._ping_task is not None
            await server.stop()
            assert server._ping_task is None

    @pytest.mark.asyncio
    async def test_a_client_that_never_handshakes_is_never_pinged(self) -> None:
        """Pings go to clients, not to sockets: a connection still owing us a
        hello is not one yet."""
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp), ping_interval=0.05)
            await server.start()

            async with connect(f"ws://127.0.0.1:{server.port}"):
                await asyncio.sleep(0.15)
                assert server._handshaken == set()

            await server.stop()


class TestReAttachIsRepeatable:
    """Resume re-attaches unconditionally, so attaching twice on one
    connection is a normal event and must stay lossless."""

    @pytest.mark.asyncio
    async def test_re_attach_supersedes_the_previous_stream(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon = Daemon(data_dir=Path(tmp))
            daemon_task = asyncio.create_task(daemon.run())
            await _await_port(daemon)

            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)

            await ws.send(json.dumps({"type": "open_workspace", "path": str(tmp_path)}))
            session_id = json.loads(await ws.recv())["session_id"]
            session = daemon.session_registry.get(session_id)
            assert session is not None

            # Open emits session_state, boundary_update, tier_state (seq 1-3).
            attach = json.dumps({"type": "attach", "session_id": session_id, "from_seq": 4})
            await ws.send(attach)
            await ws.send(attach)
            await asyncio.sleep(0.1)

            await session.event_log.add(AssistantDelta(session_id=session_id, delta="live!", seq=1))

            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            assert evt["type"] == "assistant_delta"
            assert evt["seq"] == 4

            # Exactly one copy: the second attach replaced the first stream
            # rather than running beside it.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.3)

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)
