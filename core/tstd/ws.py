"""Local WebSocket server for the TST Desk daemon.

Binds 127.0.0.1 by default, writes a port file with a random auth token,
and supports multiple simultaneous client connections. An opt-in
``remote.bind`` may add a Tailscale address on the same port — never
``0.0.0.0`` / ``::``. The port file still describes loopback.

A hello on the extra listener (or any non-loopback peer) must present
the rotating token in ``{user_data_dir}/remote-token``. The port-file
token is not enough there. Loopback hellos keep using ``hello.token``
against the port-file token — one field, two expected values.

Post-handshake messages are routed to a message handler provided by the
daemon. The handler receives parsed messages and returns responses to
send back to the client.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import websockets.exceptions
from websockets.asyncio.server import Server, ServerConnection, serve

from .logging import get_logger
from .protocol import (
    HandshakeError,
    build_error,
    build_hello_ack,
    build_ping,
    parse_hello,
    validate_hello,
    validate_token,
)
from .remote_auth import (
    connection_is_remote,
    is_loopback_host,
    remove_remote_token_file,
    write_remote_token_file,
)
from .tailscale_bind import InterfaceEnumerator, resolve_remote_bind

log = get_logger("tstd.ws")

# Token length in bytes (64 hex chars)
_TOKEN_BYTES = 32
_PORT_FILE = "port.json"

# How often the server emits the application-level ping (TD-1716).  Fast
# enough that a client can call a 30s silence a zombie after two missed
# frames; slow enough to cost nothing.
PING_INTERVAL_SECONDS = 15.0


def generate_token() -> str:
    """Generate a random hex auth token."""
    return secrets.token_hex(_TOKEN_BYTES)


def write_port_file(data_dir: Path, port: int, token: str, pid: int | None = None) -> Path:
    """Write the port, token, and PID to the port file with restricted permissions.

    If the file already exists, it is treated as stale (left by a previous
    daemon instance) and replaced — a dead daemon's port file must never
    block startup.

    The write is atomic (temp file + rename) so a supervising host polling
    the file never sees a partial read. The PID lets the host distinguish a
    live daemon's file from a stale one left by a prior, dead instance.

    Args:
        data_dir: The daemon's user data directory.
        port: The port the WebSocket server is listening on.
        token: The auth token clients must present.
        pid: The daemon's process ID. Defaults to the current process.

    Returns:
        The path to the written port file.
    """
    port_file = data_dir / _PORT_FILE
    if port_file.exists():
        log.warning(
            "stale port file detected, replacing",
            extra={"extra_fields": {"path": str(port_file)}},
        )
    content = json.dumps(
        {"port": port, "token": token, "pid": pid if pid is not None else os.getpid()},
        indent=2,
    )
    tmp_file = data_dir / (f".{_PORT_FILE}.{os.getpid()}.tmp")
    tmp_file.write_text(content)
    # Set mode 0o600 (owner read/write only) before it is renamed into place.
    # POSIX mode bits only: on Windows os.chmod can merely toggle the
    # read-only flag, so this is a silent no-op there — ACL-based
    # restriction is a separate hardening story (TD-1406).
    tmp_file.chmod(0o600)
    os.replace(tmp_file, port_file)
    log.info(
        "port file written",
        extra={"extra_fields": {"path": str(port_file), "port": port}},
    )
    return port_file


def remove_port_file(data_dir: Path) -> None:
    """Delete the port file, if present. Used on clean shutdown."""
    port_file = data_dir / _PORT_FILE
    port_file.unlink(missing_ok=True)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_UNSPECIFIED_HOSTS = frozenset({"0.0.0.0", "::", "", "*"})


def validate_interface(host: str, *, extra_allowed: str | None = None) -> None:
    """Assert that *host* is a legal bind target.

    Loopback is always allowed. ``0.0.0.0`` and ``::`` are never allowed.
    A non-loopback host is allowed only when it is exactly *extra_allowed*
    — the address ``resolve_remote_bind`` already classified as Tailscale.
    Default callers (no extra) stay loopback-only.

    Raises:
        ValueError: If host is not a permitted bind address.
    """
    if host in _UNSPECIFIED_HOSTS:
        raise ValueError(
            f"Refusing to bind to {host!r}: prime directive §2.1 requires "
            f"loopback only (127.0.0.1, ::1, or localhost). "
            f"This is enforced by the server, not by convention."
        )
    if host in _LOOPBACK_HOSTS:
        return
    if (
        extra_allowed is not None
        and extra_allowed not in _UNSPECIFIED_HOSTS
        and host == extra_allowed
    ):
        return
    raise ValueError(
        f"Refusing to bind to {host!r}: prime directive §2.1 requires "
        f"loopback only (127.0.0.1, ::1, or localhost). "
        f"This is enforced by the server, not by convention."
    )


class WebSocketServer:
    """Async WebSocket server bound to loopback, plus optional Tailscale.

    Usage:
        server = WebSocketServer(data_dir=...)
        await server.start()
        # ... server is running, clients connect ...
        await server.stop()
    """

    def __init__(
        self,
        data_dir: Path,
        handshake_timeout: float = 10.0,
        message_handler: Callable[[str, ServerConnection], Awaitable[str | None]] | None = None,
        on_disconnect: Callable[[ServerConnection], Awaitable[None]] | None = None,
        ping_interval: float = PING_INTERVAL_SECONDS,
        bind: str = "",
        interfaces: InterfaceEnumerator | None = None,
        is_remote_connection: Callable[[ServerConnection], bool] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self._server: Server | None = None
        self._extra_server: Server | None = None
        self._token: str = ""
        self._remote_token: str = ""
        self._port: int = 0
        self._connections: set[ServerConnection] = set()
        # Connections past the handshake — the only ones a ping is meaningful
        # to, and the only ones that ever receive one.
        self._handshaken: set[ServerConnection] = set()
        self._handshake_timeout = handshake_timeout
        self._message_handler = message_handler
        self._on_disconnect = on_disconnect
        self._ping_interval = ping_interval
        self._ping_task: asyncio.Task[None] | None = None
        self._bind = bind
        self._interfaces = interfaces
        self._extra_host: str | None = None
        self._is_remote_connection = is_remote_connection

    @property
    def port(self) -> int:
        return self._port

    @property
    def token(self) -> str:
        return self._token

    @property
    def remote_token(self) -> str:
        return self._remote_token

    @property
    def extra_host(self) -> str | None:
        return self._extra_host

    def peer_is_remote(self, websocket: ServerConnection) -> bool:
        """True when this connection must present the remote-token."""
        if self._is_remote_connection is not None:
            return self._is_remote_connection(websocket)
        return connection_is_remote(
            self._extra_host,
            websocket.local_address,
            websocket.remote_address,
        )

    @property
    def bound_hosts(self) -> tuple[str, ...]:
        hosts: list[str] = []
        for server in (self._server, self._extra_server):
            if server is None:
                continue
            for sock in server.sockets:
                hosts.append(sock.getsockname()[0])
        return tuple(hosts)

    async def start(self) -> None:
        """Start the WebSocket server on loopback, and Tailscale if configured."""
        self._token = generate_token()
        extra = resolve_remote_bind(self._bind, self._interfaces)
        validate_interface("127.0.0.1")
        if extra is not None:
            validate_interface(extra, extra_allowed=extra)

        loopback = await serve(
            self._on_connect,
            "127.0.0.1",
            0,  # ephemeral port
            process_request=self._auth_middleware,
        )
        self._server = loopback
        self._port = loopback.sockets[0].getsockname()[1]

        if extra is not None:
            try:
                self._extra_server = await serve(
                    self._on_connect,
                    extra,
                    self._port,
                    process_request=self._auth_middleware,
                )
            except OSError:
                loopback.close()
                await loopback.wait_closed()
                self._server = None
                raise
            self._extra_host = extra

        write_port_file(self.data_dir, self._port, self._token)
        # A 127.0.0.1 extra is still loopback — no remote token. Tests may
        # inject is_remote_connection without binding a second host.
        if (
            extra is not None and not is_loopback_host(extra)
        ) or self._is_remote_connection is not None:
            self._issue_remote_token()

        if self._ping_interval > 0:
            self._ping_task = asyncio.create_task(self._ping_loop())

        log.info(
            "ws server started",
            extra={"extra_fields": {"port": self._port, "extra_host": self._extra_host}},
        )

    async def apply_bind(self, spec: str) -> None:
        """Rebind the extra Tailscale listener without touching loopback.

        Empty *spec* drops the extra server and the remote-token file.
        A new spec resolves, replaces the extra listener on the same
        port, and mints a remote token when the host is not loopback.
        The loopback server and port file stay put.
        """
        extra = resolve_remote_bind(spec, self._interfaces)
        self._bind = spec.strip()
        if extra == self._extra_host:
            return
        await self._stop_extra()
        if extra is None:
            return
        if self._server is None or self._port == 0:
            raise RuntimeError("cannot apply a remote bind before the loopback server is up")
        validate_interface(extra, extra_allowed=extra)
        self._extra_server = await serve(
            self._on_connect,
            extra,
            self._port,
            process_request=self._auth_middleware,
        )
        self._extra_host = extra
        if not is_loopback_host(extra) or self._is_remote_connection is not None:
            self._issue_remote_token()
        log.info(
            "ws extra listener updated",
            extra={"extra_fields": {"port": self._port, "extra_host": self._extra_host}},
        )

    async def _stop_extra(self) -> None:
        """Drop the extra listener and its remote token. Loopback stays."""
        extra = self._extra_server
        self._extra_server = None
        self._extra_host = None
        if extra is not None:
            extra.close()
            await extra.wait_closed()
        remove_remote_token_file(self.data_dir)
        self._remote_token = ""

    async def _ping_loop(self) -> None:
        """Emit an application-level ping to every handshaken client (TD-1716).

        A suspended webview (macOS App Nap on an occluded window) keeps a
        healthy socket while its JavaScript is frozen: the OS answers
        transport-level ping/pong for it, so the transport can never report
        the client dead.  This frame has to be processed by the client's own
        event loop, which makes its absence the one honest signal that the
        client stopped running — the basis of the client's zombie test on
        resume.
        """
        frame = build_ping()
        while True:
            await asyncio.sleep(self._ping_interval)
            for conn in list(self._handshaken):
                with contextlib.suppress(websockets.exceptions.ConnectionClosed):
                    await conn.send(frame)

    async def broadcast(self, payload: str) -> int:
        """Send one frame to every handshaken client. Returns how many got it.

        Unlike a session event, which goes to the connections attached to
        that session, this reaches every client that finished the handshake
        — the right shape for state that belongs to the user data dir
        rather than to a session, such as the scheduled-job list.

        A client that closed between the snapshot and the send is skipped,
        not an error: it will re-read the state when it reconnects.
        """
        sent = 0
        for conn in list(self._handshaken):
            try:
                await conn.send(payload)
            except (websockets.exceptions.ConnectionClosed, OSError, RuntimeError):
                continue
            sent += 1
        return sent

    async def stop(self) -> None:
        """Stop the WebSocket server and close all connections."""
        log.info("ws server stopping")

        if self._ping_task is not None:
            self._ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ping_task
            self._ping_task = None

        # Close all client connections (iterate over a copy to avoid
        # concurrent modification from disconnect callbacks)
        connections = list(self._connections)
        for conn in connections:
            with contextlib.suppress(websockets.exceptions.ConnectionClosed):
                await conn.close(1001, "Server shutting down")
        self._connections.clear()
        self._handshaken.clear()

        await self._stop_extra()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self._server = None

        # Clean shutdown removes the port file so the host can tell a live
        # daemon from a defunct one. The remote token is already gone with
        # the extra listener; a new file is minted on the next remote-bind.
        remove_port_file(self.data_dir)

        log.info("ws server stopped")

    def _issue_remote_token(self) -> None:
        """Mint a new remote token and replace the on-disk file.

        Called on every start that has a remote listener (or a test
        classifier). The previous file is invalid after this write.
        """
        token = generate_token()
        while token == self._token:
            token = generate_token()
        self._remote_token = token
        write_remote_token_file(self.data_dir, token)

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
            hello = parse_hello(raw)
            validate_hello(hello)
            expected = self._remote_token if self.peer_is_remote(websocket) else self._token
            validate_token(hello.token, expected)
            await websocket.send(build_hello_ack())
            self._handshaken.add(websocket)
            log.info(
                "handshake ok",
                extra={"extra_fields": {"version": hello.version}},
            )
        except HandshakeError as e:
            await self._send_and_close(websocket, e.code, e.message)
            self._connections.discard(websocket)
            return
        except TimeoutError:
            await self._send_and_close(
                websocket, "handshake_timeout", "No hello message within 10s"
            )
            self._connections.discard(websocket)
            return
        except websockets.exceptions.ConnectionClosed:
            self._connections.discard(websocket)
            return

        # Post-handshake: route messages to the daemon's message handler.
        try:
            async for raw in websocket:
                if self._message_handler is not None and isinstance(raw, str):
                    response = await self._message_handler(raw, websocket)
                    if response is not None:
                        await websocket.send(response)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._connections.discard(websocket)
            self._handshaken.discard(websocket)
            if self._on_disconnect is not None:
                await self._on_disconnect(websocket)
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


def read_port_file(data_dir: Path) -> dict[str, Any] | None:
    """Read ``port.json`` if it exists and is a JSON object.

    Returns None when the file is missing or unreadable. Field checks
    (live pid, non-empty token) are the client's: this is the same
    rendezvous the host reads, not a liveness verdict.
    """
    path = create_port_file_path(data_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data
