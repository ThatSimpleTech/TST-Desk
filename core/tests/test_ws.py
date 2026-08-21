"""Tests for the local WebSocket server."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tstd.protocol import PROTOCOL_VERSION
from tstd.remote_auth import (
    connection_is_remote,
    read_remote_token_file,
    write_remote_token_file,
)
from tstd.ws import (
    WebSocketServer,
    create_port_file_path,
    generate_token,
    read_port_file,
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

    def test_validate_interface_allows_exact_tailscale_extra(self) -> None:
        validate_interface("100.64.1.5", extra_allowed="100.64.1.5")
        with pytest.raises(ValueError, match="prime directive"):
            validate_interface("192.168.1.1", extra_allowed="100.64.1.5")
        with pytest.raises(ValueError, match="prime directive"):
            validate_interface("0.0.0.0", extra_allowed="100.64.1.5")


class TestPortFile:
    def test_write_port_file_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            pf = write_port_file(data_dir, 9999, "test-token")
            assert pf.exists()
            assert pf == data_dir / "port.json"

    def test_write_port_file_restricted_mode(self) -> None:
        """0o600 on POSIX; on Windows the chmod is a no-op, by decision.

        The port file carries the daemon's auth token, so this is the
        sharper of the two permission stories — and the answer is still
        the containing directory.  ``%LOCALAPPDATA%`` grants Full to the
        user, SYSTEM and Administrators only; an ``icacls`` call on every
        port-file write would restate that at the cost of a subprocess.
        Asserted rather than skipped so the no-op is on the record — see
        ``docs/windows.md``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            pf = write_port_file(data_dir, 9999, "test-token")
            mode = pf.stat().st_mode & 0o777
            if sys.platform == "win32":
                # os.chmod can only toggle the read-only attribute, and
                # 0o600 carries a write bit, so the file stays writable
                # and stat reports the Windows default.
                assert mode == 0o666, f"expected the Windows no-op 0o666, got {oct(mode)}"
                assert json.loads(pf.read_text())["token"] == "test-token"
            else:
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

    def test_read_port_file_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_port_file(data_dir, 4321, "abc123")
            info = read_port_file(data_dir)
            assert info is not None
            assert info["port"] == 4321
            assert info["token"] == "abc123"
            assert "pid" in info

    def test_read_port_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assert read_port_file(Path(tmp)) is None

    def test_read_port_file_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "port.json").write_text("not-json", encoding="utf-8")
            assert read_port_file(data_dir) is None


class TestRemoteTokenFile:
    def test_write_remote_token_restricted_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            path = write_remote_token_file(data_dir, "remote-secret")
            assert path == data_dir / "remote-token"
            mode = path.stat().st_mode & 0o777
            if sys.platform == "win32":
                assert mode == 0o666, f"expected the Windows no-op 0o666, got {oct(mode)}"
                assert path.read_text(encoding="utf-8") == "remote-secret"
            else:
                assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
            assert read_remote_token_file(data_dir) == "remote-secret"

    def test_remote_token_is_not_the_port_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_port_file(data_dir, 1, "port-token")
            write_remote_token_file(data_dir, "remote-token-value")
            assert json.loads((data_dir / "port.json").read_text())["token"] == "port-token"
            assert read_remote_token_file(data_dir) == "remote-token-value"

    def test_write_does_not_log_the_token(self, caplog: pytest.LogCaptureFixture) -> None:
        token = generate_token()
        with (
            tempfile.TemporaryDirectory() as tmp,
            caplog.at_level("INFO", logger="tstd.remote_auth"),
        ):
            write_remote_token_file(Path(tmp), token)
        assert token not in caplog.text


class TestConnectionIsRemote:
    def test_loopback_extra_is_not_remote(self) -> None:
        assert connection_is_remote("127.0.0.1", ("127.0.0.1", 9), ("127.0.0.1", 10)) is False
        assert connection_is_remote("::1", ("::1", 9), ("::1", 10)) is False

    def test_extra_host_listener_is_remote(self) -> None:
        assert connection_is_remote("100.64.1.5", ("100.64.1.5", 9), ("100.64.2.3", 10)) is True

    def test_loopback_peer_on_loopback_socket_stays_local(self) -> None:
        assert connection_is_remote("100.64.1.5", ("127.0.0.1", 9), ("127.0.0.1", 10)) is False

    def test_non_loopback_peer_is_remote(self) -> None:
        assert connection_is_remote(None, ("127.0.0.1", 9), ("100.64.1.5", 10)) is True


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
        """Default start is loopback-only. A LAN bind is the new refuse path."""
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(Path(tmp))
            await server.start()
            assert server.port > 0
            assert server.extra_host is None
            assert "0.0.0.0" not in server.bound_hosts
            assert "::" not in server.bound_hosts
            assert all(host in {"127.0.0.1", "::1"} for host in server.bound_hosts)
            await server.stop()

        with tempfile.TemporaryDirectory() as tmp:
            refused = WebSocketServer(
                Path(tmp),
                bind="192.168.1.1",
                interfaces=lambda: {"eth0": ("192.168.1.1",)},
            )
            with pytest.raises(ValueError, match="not a Tailscale"):
                await refused.start()
            assert refused.extra_host is None

        with tempfile.TemporaryDirectory() as tmp:
            unspecified = WebSocketServer(Path(tmp), bind="0.0.0.0")
            with pytest.raises(ValueError, match=r"never 0\.0\.0\.0"):
                await unspecified.start()

    @pytest.mark.asyncio
    async def test_opt_in_bind_listens_on_loopback_and_tailscale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """remote.bind names an iface; both loopback and that address listen.

        serve is faked so a live Tailscale address is not required. The
        recorded hosts are the contract: loopback + Tailscale, never 0.0.0.0.
        """
        recorded: list[tuple[str, int]] = []

        class _FakeSock:
            def __init__(self, host: str, port: int) -> None:
                self._host = host
                self._port = port

            def getsockname(self) -> tuple[str, int]:
                return (self._host, self._port)

        class _FakeServer:
            def __init__(self, host: str, port: int) -> None:
                self.sockets = [_FakeSock(host, port)]

            def close(self) -> None:
                return None

            async def wait_closed(self) -> None:
                return None

        async def fake_serve(_handler: object, host: str, port: int, **_kwargs: Any) -> _FakeServer:
            bound = 54321 if port == 0 else port
            recorded.append((host, bound))
            return _FakeServer(host, bound)

        monkeypatch.setattr("tstd.ws.serve", fake_serve)
        extra = "100.64.1.5"
        with tempfile.TemporaryDirectory() as tmp:
            server = WebSocketServer(
                Path(tmp),
                bind="tailscale0",
                interfaces=lambda: {"tailscale0": (extra,)},
                ping_interval=0,
            )
            await server.start()
            try:
                assert recorded == [("127.0.0.1", 54321), (extra, 54321)]
                assert "0.0.0.0" not in {host for host, _ in recorded}
                assert "::" not in {host for host, _ in recorded}
                assert server.extra_host == extra
                info = read_port_file(Path(tmp))
                assert info is not None
                assert info["port"] == server.port == 54321
            finally:
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
