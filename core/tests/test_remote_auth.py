"""TD-3602: non-loopback hello needs a rotating user-data-dir token."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from tstd.protocol import PROTOCOL_VERSION
from tstd.remote_auth import read_remote_token_file
from tstd.ws import WebSocketServer


class TestRemoteAuth:
    @pytest.mark.asyncio
    async def test_loopback_keeps_port_file_token(self) -> None:
        """Loopback hello still authenticates with the port-file token."""
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp), ping_interval=0)
            await server.start()
            try:
                assert not server.remote_token
                assert read_remote_token_file(Path(tmp)) is None
                uri = f"ws://127.0.0.1:{server.port}"
                async with connect(uri) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "hello",
                                "token": server.token,
                                "version": PROTOCOL_VERSION,
                            }
                        )
                    )
                    msg = json.loads(await ws.recv())
                    assert msg["type"] == "hello_ack"
            finally:
                await server.stop()

    @pytest.mark.asyncio
    async def test_remote_rejects_port_file_token(self) -> None:
        """A leaked port file is not enough on a remote-classified connection."""
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(
                Path(tmp),
                ping_interval=0,
                is_remote_connection=lambda _ws: True,
            )
            await server.start()
            try:
                uri = f"ws://127.0.0.1:{server.port}"
                async with connect(uri) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "hello",
                                "token": server.token,
                                "version": PROTOCOL_VERSION,
                            }
                        )
                    )
                    msg = json.loads(await ws.recv())
                    assert msg["type"] == "error"
                    assert msg["code"] == "auth_failed"
            finally:
                await server.stop()

    @pytest.mark.asyncio
    async def test_remote_accepts_remote_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(
                Path(tmp),
                ping_interval=0,
                is_remote_connection=lambda _ws: True,
            )
            await server.start()
            try:
                on_disk = read_remote_token_file(Path(tmp))
                assert on_disk == server.remote_token
                assert server.remote_token != server.token
                uri = f"ws://127.0.0.1:{server.port}"
                async with connect(uri) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "hello",
                                "token": server.remote_token,
                                "version": PROTOCOL_VERSION,
                            }
                        )
                    )
                    msg = json.loads(await ws.recv())
                    assert msg["type"] == "hello_ack"
            finally:
                await server.stop()

    @pytest.mark.asyncio
    async def test_remote_token_rotates_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            first = WebSocketServer(data, ping_interval=0, is_remote_connection=lambda _ws: True)
            await first.start()
            token1 = first.remote_token
            await first.stop()

            second = WebSocketServer(data, ping_interval=0, is_remote_connection=lambda _ws: True)
            await second.start()
            token2 = second.remote_token
            try:
                assert token1 != token2
                uri = f"ws://127.0.0.1:{second.port}"
                async with connect(uri) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "hello",
                                "token": token1,
                                "version": PROTOCOL_VERSION,
                            }
                        )
                    )
                    msg = json.loads(await ws.recv())
                    assert msg["type"] == "error"
                    assert msg["code"] == "auth_failed"
            finally:
                await second.stop()

    @pytest.mark.asyncio
    async def test_failed_remote_auth_is_typed_close_not_session(self) -> None:
        """Wrong remote hello closes with auth_failed and never reaches a handler."""
        handled: list[str] = []

        async def handler(raw: str, _ws: object) -> str | None:
            handled.append(raw)
            return None

        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(
                Path(tmp),
                ping_interval=0,
                message_handler=handler,
                is_remote_connection=lambda _ws: True,
            )
            await server.start()
            try:
                uri = f"ws://127.0.0.1:{server.port}"
                async with connect(uri) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "hello",
                                "token": server.token,
                                "version": PROTOCOL_VERSION,
                            }
                        )
                    )
                    msg = json.loads(await ws.recv())
                    assert msg["type"] == "error"
                    assert msg["code"] == "auth_failed"
                assert handled == []
                for _ in range(20):
                    if server.client_count == 0:
                        break
                    await asyncio.sleep(0.05)
                assert server.client_count == 0
            finally:
                await server.stop()

    @pytest.mark.asyncio
    async def test_failed_remote_auth_creates_no_daemon_session(self) -> None:
        from tstd.daemon import Daemon

        with tempfile.TemporaryDirectory() as tmp:
            daemon = Daemon(data_dir=Path(tmp))
            daemon.ws_server._is_remote_connection = lambda _ws: True
            task = asyncio.create_task(daemon.run())
            try:
                for _ in range(50):
                    if daemon.ws_server.port:
                        break
                    await asyncio.sleep(0.05)
                assert daemon.ws_server.port > 0
                assert daemon.ws_server.remote_token
                uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
                async with connect(uri) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "hello",
                                "token": daemon.ws_server.token,
                                "version": PROTOCOL_VERSION,
                            }
                        )
                    )
                    msg = json.loads(await ws.recv())
                    assert msg["type"] == "error"
                    assert msg["code"] == "auth_failed"
                assert daemon.state.active_sessions == 0
                assert await daemon.session_registry.list_sessions() == []
            finally:
                daemon._shutdown_event.set()
                await asyncio.gather(task, return_exceptions=True)
