"""Local WebSocket server for the TST Desk daemon.

Binds 127.0.0.1 only, writes a port file with a random auth token,
and supports multiple simultaneous client connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from pathlib import Path
from typing import Any

import websockets.exceptions
from websockets.asyncio.server import Server, ServerConnection, serve

from .logging import get_logger
from .protocol import (
    HandshakeError,
    HelloMessage,
    build_error,
    build_hello_ack,
    validate_token,
    validate_version,
)

log = get_logger("tstd.ws")

# Token length in bytes (64 hex chars)
_TOKEN_BYTES = 32
_PORT_FILE = "port.json"


def generate_token() -> str:
    """Generate a random hex auth token."""
    return secrets.token_hex(_TOKEN_BYTES)


def write_port_file(data_dir: Path, port: int, token: str) -> Path:
    """Write the port and token to the port file with restricted permissions.

    Args:
        data_dir: The daemon's user data directory.
        port: The port the WebSocket server is listening on.
        token: The auth token clients must present.

    Returns:
        The path to the written port file.
    """
    port_file = data_dir / _PORT_FILE
    content = json.dumps({"port": port, "token": token}, indent=2)
    port_file.write_text(content)
    # Set mode 0o600 (owner read/write only)
    port_file.chmod(0o600)
    log.info(
        "port file written",
        extra={"extra_fields": {"path": str(port_file), "port": port}},
    )
    return port_file


def validate_interface(host: str) -> None:
    """Assert that the server binds only to a loopback interface.

    Raises:
        ValueError: If host is not a loopback address.
    """
    loopback = {"127.0.0.1", "::1", "localhost"}
    if host not in loopback:
        raise ValueError(
            f"Refusing to bind to {host!r}: prime directive §2.1 requires "
            f"loopback only (127.0.0.1, ::1, or localhost). "
            f"This is enforced by the server, not by convention."
        )


class WebSocketServer:
    """Async WebSocket server bound to loopback with an auth token.

    Usage:
        server = WebSocketServer(data_dir=...)
        await server.start()
        # ... server is running, clients connect ...
        await server.stop()
    """

    def __init__(self, data_dir: Path, handshake_timeout: float = 10.0) -> None:
        self.data_dir = data_dir
        self._server: Server | None = None
        self._token: str = ""
        self._port: int = 0
        self._connections: set[ServerConnection] = set()
        self._handshake_timeout = handshake_timeout

    @property
    def port(self) -> int:
        return self._port

    @property
    def token(self) -> str:
        return self._token

    async def start(self) -> None:
        """Start the WebSocket server on an ephemeral loopback port."""
        self._token = generate_token()
        validate_interface("127.0.0.1")

        self._server = await serve(
            self._on_connect,
            "127.0.0.1",
            0,  # ephemeral port
            process_request=self._auth_middleware,
        )
        self._port = self._server.sockets[0].getsockname()[1]

        write_port_file(self.data_dir, self._port, self._token)

        log.info(
            "ws server started",
            extra={"extra_fields": {"port": self._port}},
        )

    async def stop(self) -> None:
        """Stop the WebSocket server and close all connections."""
        log.info("ws server stopping")

        # Close all client connections
        for conn in self._connections:
            await conn.close(1001, "Server shutting down")
        self._connections.clear()

        # Close the server
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

        log.info("ws server stopped")

    async def _on_connect(self, websocket: ServerConnection) -> None:
        """Handle a new client connection, requiring a token handshake."""
        self._connections.add(websocket)
        log.info(
            "client connected",
            extra={"extra_fields": {"remote": str(websocket.remote_address)}},
        )

        # Handshake: the first message must be a valid `hello`.
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=self._handshake_timeout)
            if not isinstance(raw, str):
                raise HandshakeError("bad_request", "Handshake must be text")
            hello = HelloMessage.parse(raw)
            validate_version(hello.version)
            validate_token(hello.token, self._token)
            await websocket.send(build_hello_ack())
            log.info(
                "handshake ok",
                extra={"extra_fields": {"version": hello.version}},
            )
        except HandshakeError as e:
            await self._send_and_close(websocket, e.code, e.message)
            return
        except TimeoutError:
            await self._send_and_close(
                websocket, "handshake_timeout", "No hello message within 10s"
            )
            return
        except websockets.exceptions.ConnectionClosed:
            return

        # Post-handshake: messages pass through untouched (protocol in TD-204).
        try:
            async for _message in websocket:
                pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._connections.discard(websocket)
            log.info("client disconnected")

    async def _send_and_close(self, websocket: ServerConnection, code: str, message: str) -> None:
        """Send a typed error and close the connection."""
        log.warning(
            "handshake rejected",
            extra={"extra_fields": {"code": code, "message": message}},
        )
        with contextlib.suppress(websockets.exceptions.ConnectionClosed):
            await websocket.send(build_error(code, message))
        await websocket.close(1008, code)

    async def _auth_middleware(self, _connection: ServerConnection, _request: Any) -> None:
        """Accept connections; the token handshake is handled in TD-203.

        Returns None to allow the connection.
        """
        return None

    @property
    def client_count(self) -> int:
        return len(self._connections)


def create_port_file_path(data_dir: Path) -> Path:
    """Return the path to the port file in the given data directory."""
    return data_dir / _PORT_FILE
