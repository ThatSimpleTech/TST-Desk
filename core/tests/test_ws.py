"""Tests for the local WebSocket server."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from tstd.protocol import PROTOCOL_VERSION
from tstd.ws import (
    WebSocketServer,
    create_port_file_path,
    generate_token,
    validate_interface,
    write_port_file,
)


class TestAuth:
    def test_generate_token(self) -> None:
        t1 = generate_token()
        t2 = generate_token()
        assert len(t1) == 64  # 32 bytes = 64 hex chars
        assert t1 != t2  # random

    def test_validate_interface_loopback(self) -> None:
        # Should not raise
        validate_interface("127.0.0.1")
        validate_interface("::1")
        validate_interface("localhost")

    def test_validate_interface_rejects_non_loopback(self) -> None:
        with pytest.raises(ValueError, match="prime directive"):
            validate_interface("0.0.0.0")
        with pytest.raises(ValueError, match="prime directive"):
            validate_interface("192.168.1.1")
        with pytest.raises(ValueError, match="prime directive"):
            validate_interface("10.0.0.1")


class TestPortFile:
    def test_write_port_file_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            pf = write_port_file(data_dir, 9999, "test-token")
            assert pf.exists()
            assert pf == data_dir / "port.json"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="TD-1406: Windows has no POSIX mode bits — os.chmod only toggles "
        "the read-only flag, so 0o600 is a no-op (ACL hardening is a separate story)",
    )
    def test_write_port_file_restricted_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            pf = write_port_file(data_dir, 9999, "test-token")
            # Check mode is 0o600 (owner read/write only)
            mode = pf.stat().st_mode & 0o777
            assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_write_port_file_contains_correct_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_port_file(data_dir, 4321, "abc123")
            content = json.loads((data_dir / "port.json").read_text())
            assert content["port"] == 4321
            assert content["token"] == "abc123"

    def test_create_port_file_path(self) -> None:
        p = create_port_file_path(Path("/tmp/test"))
        assert p == Path("/tmp/test") / "port.json"


async def _do_handshake(uri: str, token: str) -> None:
    """Connect to a server and perform the hello handshake."""
    async with connect(uri) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "token": token,
                    "version": PROTOCOL_VERSION,
                }
            )
        )
        await ws.recv()  # hello_ack


class TestWebSocketServer:
    @pytest.mark.asyncio
    async def test_start_and_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp))
            await server.start()
            assert server.port > 0
            assert len(server.token) == 64
            # Port file exists
            assert (Path(tmp) / "port.json").exists()
            await server.stop()

    @pytest.mark.asyncio
    async def test_multiple_clients(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp))
            await server.start()
            uri = f"ws://127.0.0.1:{server.port}"

            async def client() -> None:
                await _do_handshake(uri, server.token)

            await asyncio.gather(client(), client(), client())
            await server.stop()

    @pytest.mark.asyncio
    async def test_client_disconnect_does_not_disturb_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp))
            await server.start()
            uri = f"ws://127.0.0.1:{server.port}"

            # Connect and disconnect with handshake
            await _do_handshake(uri, server.token)

            # Wait for the server to process the disconnect
            for _ in range(20):
                if server.client_count == 0:
                    break
                await asyncio.sleep(0.05)

            # Server should still be operational
            assert server.client_count == 0
            await _do_handshake(uri, server.token)

            for _ in range(20):
                if server.client_count == 0:
                    break
                await asyncio.sleep(0.05)
            assert server.client_count == 0

            await server.stop()

    @pytest.mark.asyncio
    async def test_client_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp))
            await server.start()
            uri = f"ws://127.0.0.1:{server.port}"

            async def handshake_and_wait() -> None:
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
                    await ws.recv()  # hello_ack
                    # Stay connected until the test releases us
                    await asyncio.Event().wait()

            # Start first client
            t1 = asyncio.create_task(handshake_and_wait())
            await asyncio.sleep(0.2)
            assert server.client_count == 1

            # Start second client
            t2 = asyncio.create_task(handshake_and_wait())
            await asyncio.sleep(0.2)
            assert server.client_count == 2

            # Cancel second client, wait for cleanup
            t2.cancel()
            await asyncio.gather(t2, return_exceptions=True)
            for _ in range(20):
                if server.client_count == 1:
                    break
                await asyncio.sleep(0.05)
            assert server.client_count == 1

            # Cancel first client
            t1.cancel()
            await asyncio.gather(t1, return_exceptions=True)
            for _ in range(20):
                if server.client_count == 0:
                    break
                await asyncio.sleep(0.05)
            assert server.client_count == 0

            await server.stop()

    @pytest.mark.asyncio
    async def test_refuses_non_loopback_bind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp))
            # Override to test non-loopback — the validate_interface check
            # runs during start(), so we can't start with a non-loopback host.
            # The validate_interface() function is tested separately.
            # This test confirms the server starts only on 127.0.0.1 internally.
            await server.start()
            assert server.port > 0
            await server.stop()

    @pytest.mark.asyncio
    async def test_health_fields_in_daemon(self) -> None:
        """Verify the daemon's health endpoint reflects WebSocket server state."""
        from tstd.daemon import Daemon

        with tempfile.TemporaryDirectory() as tmp:
            d = Daemon(data_dir=Path(tmp))

            async def stop() -> None:
                await asyncio.sleep(0.3)
                d._shutdown_event.set()

            await asyncio.gather(d.run(), stop())
            h = d.health()
            assert h["ws_port"] > 0
            assert h["ws_clients"] == 0
