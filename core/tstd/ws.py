"""Local WebSocket server for the TST Desk daemon.

Binds 127.0.0.1 by default, writes a port file with a random auth token,
and supports multiple simultaneous client connections. An opt-in
``remote.bind`` may add a Tailscale address on the same port — never
``0.0.0.0`` / ``::``. The port file still describes loopback.

A hello on the extra listener (or any non-loopback peer) must present
the rotating token in ``{user_data_dir}/remote-token``. The port-file
token is not enough there. Loopback hellos keep using ``hello.token``
against the port-file token — one field, two expected values.

A paired device (TD-5014) presents its own key there instead. A remote
peer pairs by sending ``pair`` as its first frame: the one-time code is
spent, the device is minted a key in ``pair_ok``, and the socket closes.
A ``pair`` carrying ``replaces`` (the phone's old key) also retires the
device that key belongs to. Codes and keys are never logged.

While the paired devices file cannot be read, a remote hello that is not
the legacy token gets ``devices_unavailable`` (keep the key, retry), and
each one retries the read. A device connecting or dropping calls
``on_device_presence`` so the computer's windows can refresh the list.

Post-handshake messages are routed to a message handler provided by the
daemon. The handler receives parsed messages and returns responses to
send back to the client.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

import websockets.exceptions
from websockets.asyncio.server import Server, ServerConnection, serve

from .identity import load_or_mint_machine_id
from .logging import get_logger
from .peer_policy import LOOPBACK_PLAIN, PeerView, classify
from .port_file import PORT_FILE_NAME as _PORT_FILE
from .port_file import release_port_file, write_port_file
from .protocol import (
    HandshakeError,
    RemoteDeviceSummary,
    build_error,
    build_hello_ack,
    build_pair_ok,
    build_ping,
    handshake_type,
    parse_hello,
    parse_pair,
    validate_hello,
    validate_token,
    validate_version,
)
from .remote_auth import (
    REMOTE_TOKEN_FILE,
    connection_is_remote,
    is_loopback_host,
    read_remote_token_file,
    remove_remote_token_file,
    write_remote_token_file,
)
from .remote_devices import (
    DEVICES_UNAVAILABLE_MESSAGE,
    PAIR_FAILED_MESSAGE,
    PAIR_LOCKED_MESSAGE,
    DeviceLimitError,
    PairingCodes,
    RemoteDeviceRegistry,
    desktop_name,
)
from .tailscale_bind import InterfaceEnumerator, resolve_remote_bind

log = get_logger("tstd.ws")

# Who a broadcast reaches (TD-5015). Machine peers never get one.
Audience = Literal["all", "viewers", "loopback"]

# Token length in bytes (64 hex chars)
_TOKEN_BYTES = 32

# How often the server emits the application-level ping (TD-1716).  Fast
# enough that a client can call a 30s silence a zombie after two missed
# frames; slow enough to cost nothing.
PING_INTERVAL_SECONDS = 15.0

# Host grace is 5s. The library's close handshake defaults to 10s, so
# one peer that never acks used to be killed with the port file still
# on disk. One second fits two clients inside that grace.
_SHUTDOWN_CLOSE_SECONDS = 1.0


def generate_token() -> str:
    """Generate a random hex auth token."""
    return secrets.token_hex(_TOKEN_BYTES)


def _is_token(value: str) -> bool:
    """True for a token this module could have minted (64 hex chars)."""
    if len(value) != 2 * _TOKEN_BYTES:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


async def _close_client(
    conn: ServerConnection, code: int = 1001, reason: str = "Server shutting down"
) -> None:
    """Close one client without waiting out a peer that never acks."""
    conn.close_timeout = _SHUTDOWN_CLOSE_SECONDS
    with contextlib.suppress(
        websockets.exceptions.ConnectionClosed,
        websockets.exceptions.ConcurrencyError,
        OSError,
    ):
        await conn.close(code, reason)


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
        ping_interval: float | None = None,
        bind: str = "",
        interfaces: InterfaceEnumerator | None = None,
        is_remote_connection: Callable[[ServerConnection], bool] | None = None,
        port: int = 0,
        devices: RemoteDeviceRegistry | None = None,
        pairing: PairingCodes | None = None,
        on_paired: Callable[[RemoteDeviceSummary], Awaitable[None]] | None = None,
        on_device_presence: Callable[[str], Awaitable[None]] | None = None,
        on_handshake: Callable[[ServerConnection], Awaitable[None]] | None = None,
        machine_id: str | None = None,
    ) -> None:
        self.data_dir = data_dir
        # 0 is a random port. A fixed one keeps a saved phone working
        # across restarts (TD-5005); if it is taken, start() falls back.
        self._preferred_port = port
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
        # Resolved at init so a test can quiet the cadence by patching
        # PING_INTERVAL_SECONDS without restating every constructor (TD-1716).
        self._ping_interval = PING_INTERVAL_SECONDS if ping_interval is None else ping_interval
        self._ping_task: asyncio.Task[None] | None = None
        self._bind = bind
        self._interfaces = interfaces
        self._extra_host: str | None = None
        self._is_remote_connection = is_remote_connection
        # TD-5014: the daemon passes the registry it loaded; a bare server
        # (tests) reads the same file itself.
        if devices is None:
            devices = RemoteDeviceRegistry(data_dir)
            devices.load()
        self._devices = devices
        self._pairing = pairing if pairing is not None else PairingCodes()
        self._on_paired = on_paired
        self._on_device_presence = on_device_presence
        # Handshaken connections that presented a device key, by device id.
        self._device_of: dict[ServerConnection, str] = {}
        # Set by stop(): the closes it causes are not presence worth telling.
        self._stopping = False
        # TD-5015: what each handshaken connection is (viewer, machine peer).
        self._peers: dict[ServerConnection, PeerView] = {}
        self._on_handshake = on_handshake
        self._machine_id = machine_id

    @property
    def port(self) -> int:
        return self._port

    @property
    def preferred_port(self) -> int:
        """The port start() tried first; 0 when it went straight to a random one."""
        return self._preferred_port

    @property
    def token(self) -> str:
        return self._token

    @property
    def remote_token(self) -> str:
        return self._remote_token

    @property
    def extra_host(self) -> str | None:
        return self._extra_host

    @property
    def devices(self) -> RemoteDeviceRegistry:
        return self._devices

    @property
    def pairing(self) -> PairingCodes:
        return self._pairing

    @property
    def machine_id(self) -> str | None:
        """This computer's identity (``mid_``), minted at start()."""
        return self._machine_id

    def peer_view(self, websocket: ServerConnection) -> PeerView:
        """What this connection is. One not handshaken here: plain loopback."""
        return self._peers.get(websocket, LOOPBACK_PLAIN)

    def viewer_connections(self) -> list[ServerConnection]:
        return [conn for conn, peer in self._peers.items() if peer.viewer]

    def connected_device_ids(self) -> set[str]:
        """Paired devices with at least one live, handshaken connection."""
        return set(self._device_of.values())

    def device_of(self, websocket: ServerConnection) -> str | None:
        """The paired device this connection authenticated as, if any."""
        return self._device_of.get(websocket)

    async def close_device(self, device_id: str) -> int:
        """Close every connection of one device (TD-5014). Returns how many."""
        conns = [conn for conn, dev in self._device_of.items() if dev == device_id]
        # Untracked now, so the next setup_state already reads disconnected.
        for conn in conns:
            self._device_of.pop(conn, None)
        if conns:
            await asyncio.gather(*(_close_client(c, 1008, "device removed") for c in conns))
        return len(conns)

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
        self._stopping = False
        self._token = generate_token()
        if self._machine_id is None:
            self._machine_id = load_or_mint_machine_id(self.data_dir)
        extra = resolve_remote_bind(self._bind, self._interfaces)
        validate_interface("127.0.0.1")
        if extra is not None:
            validate_interface(extra, extra_allowed=extra)

        loopback = await self._serve_loopback()
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
        await self._stop_extra(revoke=extra is None)
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

    async def _stop_extra(self, *, revoke: bool) -> None:
        """Drop the extra listener. Loopback stays.

        *revoke* also deletes the remote-token file: remote attach turned
        off. A shutdown or a host change keeps it (TD-5005).
        """
        extra = self._extra_server
        self._extra_server = None
        self._extra_host = None
        if extra is not None:
            extra.close()
            await extra.wait_closed()
        if revoke:
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

    async def broadcast(
        self, payload: str, *, loopback_only: bool = False, audience: Audience = "all"
    ) -> int:
        """Send one frame to every handshaken client. Returns how many got it.

        Unlike a session event, which goes to the connections attached to
        that session, this reaches every client that finished the handshake
        — the right shape for state that belongs to the user data dir
        rather than to a session, such as the scheduled-job list.
        *loopback_only* skips remote peers (TD-5014's ``pairing_completed``);
        it is ``audience="loopback"``. ``"viewers"`` reaches viewers only.
        Machine peers never get a broadcast (TD-5015).

        A client that closed between the snapshot and the send is skipped,
        not an error: it will re-read the state when it reconnects.
        """
        sent = 0
        if loopback_only:
            audience = "loopback"
        for conn in list(self._handshaken):
            peer = self.peer_view(conn)
            if peer.machine or (audience == "viewers" and not peer.viewer):
                continue
            if audience == "loopback" and self.peer_is_remote(conn):
                continue
            try:
                await conn.send(payload)
            except (websockets.exceptions.ConnectionClosed, OSError, RuntimeError):
                continue
            sent += 1
        return sent

    async def stop(self) -> None:
        """Stop the WebSocket server and close all connections.

        The port file is released before any await. It used to be
        unlinked only after every client finished its close handshake,
        and a peer that never acked outlived the host's kill grace.
        Releasing again at the end would delete a successor's file.
        The remote token goes with the extra listener.
        """
        log.info("ws server stopping")
        self._stopping = True
        release_port_file(self.data_dir)
        # Same turn as server.close(), before any await: the library's
        # close task reads this when it runs, and the default is 10s.
        # ``connections`` covers a socket that finished the HTTP upgrade
        # but has not reached ``_on_connect`` yet.
        armed = set(self._connections)
        for server in (self._server, self._extra_server):
            # Test doubles stand in for the listener and have no client set.
            if isinstance(server, Server):
                armed.update(server.connections)
        for conn in armed:
            conn.close_timeout = _SHUTDOWN_CLOSE_SECONDS
        # Stop accepting before the close wait so a new handshake cannot
        # attach and hold ``wait_closed`` at the library's 10s default.
        if self._server is not None:
            self._server.close()
        if self._extra_server is not None:
            self._extra_server.close()

        if self._ping_task is not None:
            self._ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ping_task
            self._ping_task = None

        connections = list(self._connections)
        if connections:
            await asyncio.gather(*(_close_client(conn) for conn in connections))
        self._connections.clear()
        self._handshaken.clear()
        self._device_of.clear()
        self._peers.clear()

        await self._stop_extra(revoke=False)
        if self._server is not None:
            await self._server.wait_closed()
        self._server = None

        log.info("ws server stopped")

    async def _serve_loopback(self) -> Server:
        """Bind loopback on the preferred port, or a random one if it is busy."""
        if self._preferred_port:
            try:
                return await serve(
                    self._on_connect,
                    "127.0.0.1",
                    self._preferred_port,
                    process_request=self._auth_middleware,
                )
            except OSError:
                log.warning(
                    "preferred port busy; using a random port",
                    extra={"extra_fields": {"port": self._preferred_port}},
                )
        return await serve(
            self._on_connect,
            "127.0.0.1",
            0,  # ephemeral port
            process_request=self._auth_middleware,
        )

    def _issue_remote_token(self) -> None:
        """Load the saved remote token, or mint one if there is none.

        The token survives restarts so a saved phone keeps working
        (TD-5005). It changes only on :meth:`rotate_remote_token` or when
        remote attach is turned off, which deletes the file.
        """
        saved = read_remote_token_file(self.data_dir)
        if saved is not None and _is_token(saved) and saved != self._token:
            self._remote_token = saved
            (self.data_dir / REMOTE_TOKEN_FILE).chmod(0o600)
            return
        self._mint_remote_token()

    def _mint_remote_token(self) -> None:
        token = generate_token()
        while token == self._token:
            token = generate_token()
        self._remote_token = token
        write_remote_token_file(self.data_dir, token)

    async def rotate_remote_token(self) -> bool:
        """Replace the remote token and drop its remote clients (TD-5005).

        Returns False when no remote listener is up, so there is no token
        to replace. A phone still holding the old token must be given
        the new one. Paired devices keep their own keys and stay (TD-5014).
        """
        if self._extra_host is None or not self._remote_token:
            return False
        self._mint_remote_token()
        remote = [
            c for c in self._connections if self.peer_is_remote(c) and c not in self._device_of
        ]
        if remote:
            await asyncio.gather(*(_close_client(conn) for conn in remote))
        log.info("remote token rotated", extra={"extra_fields": {"dropped": len(remote)}})
        return True

    async def _on_connect(self, websocket: ServerConnection) -> None:
        """Handle a new client connection, requiring a token handshake."""
        self._connections.add(websocket)
        log.info(
            "client connected",
            extra={"extra_fields": {"remote": str(websocket.remote_address)}},
        )

        # Handshake: the first message must be a valid `hello`, or a
        # `pair` from a remote peer redeeming a code (TD-5014).
        device_id: str | None = None
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=self._handshake_timeout)
            if not isinstance(raw, str):
                raise HandshakeError("bad_request", "Handshake must be text")
            if handshake_type(raw) == "pair":
                await self._redeem_pairing(websocket, raw)
                self._connections.discard(websocket)
                return
            hello = parse_hello(raw)
            validate_hello(hello)
            device_id = self._authenticate(websocket, hello.token)
            await websocket.send(
                build_hello_ack(machine_id=self._machine_id, desktop_name=desktop_name())
            )
            self._handshaken.add(websocket)
            kind = self._devices.kind_of(device_id) if device_id is not None else None
            self._peers[websocket] = classify(self.peer_is_remote(websocket), kind, hello.features)
            if device_id is not None:
                self._device_of[websocket] = device_id
                self._devices.touch_last_seen(device_id)
            log.info(
                "handshake ok",
                extra={"extra_fields": {"version": hello.version, "device_id": device_id}},
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
            if device_id is not None:
                await self._announce_presence(device_id)
            if self._on_handshake is not None:
                await self._on_handshake(websocket)
            async for raw in websocket:
                current = self._device_of.get(websocket)
                if current is not None:
                    self._devices.touch_last_seen(current)
                if self._message_handler is not None and isinstance(raw, str):
                    response = await self._message_handler(raw, websocket)
                    if response is not None:
                        await websocket.send(response)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._connections.discard(websocket)
            self._handshaken.discard(websocket)
            self._device_of.pop(websocket, None)
            self._peers.pop(websocket, None)
            if self._on_disconnect is not None:
                await self._on_disconnect(websocket)
            # The id from the handshake, not the map: close_device() untracks
            # a removed device first, and its going is still news.
            if device_id is not None:
                self._devices.touch_last_seen(device_id)
                await self._announce_presence(device_id)
            log.info("client disconnected")

    async def _announce_presence(self, device_id: str) -> None:
        if self._on_device_presence is not None and not self._stopping:
            await self._on_device_presence(device_id)

    def _authenticate(self, websocket: ServerConnection, token: str) -> str | None:
        """Check a hello's token. Returns the device id when it is a device key.

        Loopback: the port-file token only. Remote: a paired device's key
        (TD-5014) or the legacy ``remote-token`` (TD-3602).

        An unreadable devices file is read again first. Still unreadable,
        anything but the legacy token is ``devices_unavailable``, not
        ``auth_failed``: the phone's key may be fine, so it keeps it.
        """
        if not self.peer_is_remote(websocket):
            validate_token(token, self._token)
            return None
        if not self._devices.available:
            self._devices.load()
        device_id = self._devices.verify(token)
        if device_id is not None:
            return device_id
        try:
            validate_token(token, self._remote_token)
        except HandshakeError:
            if self._devices.available:
                raise
            raise HandshakeError("devices_unavailable", DEVICES_UNAVAILABLE_MESSAGE) from None
        return None

    async def _redeem_pairing(self, websocket: ServerConnection, raw: str) -> None:
        """First-frame ``pair`` (TD-5014): spend the code, mint this device a key.

        Success sends ``pair_ok`` and closes 1000; the phone reconnects
        with ``hello``. Failures raise the typed handshake error. Only
        then is the device ``replaces`` names retired, so a phone that
        never got its new key still has a working old one.
        """
        if not self.peer_is_remote(websocket):
            raise HandshakeError(
                "bad_request",
                "This computer is already connected; pairing is for other devices.",
            )
        pair = parse_pair(raw)
        validate_version(pair.version)
        result = self._pairing.redeem(pair.code)
        if result == "locked":
            raise HandshakeError("pair_locked", PAIR_LOCKED_MESSAGE)
        if result != "ok":
            raise HandshakeError("pair_failed", PAIR_FAILED_MESSAGE)
        # The legacy token verifies to no device, so it retires nothing.
        replaced = self._devices.verify(pair.replaces) if pair.replaces else None
        try:
            device, token = self._devices.add(
                pair.device_name, avoid=(self._token, self._remote_token), kind=pair.kind
            )
        except (DeviceLimitError, OSError) as e:
            raise HandshakeError("pair_failed", PAIR_FAILED_MESSAGE) from e
        try:
            await websocket.send(
                build_pair_ok(device.id, device.name, token, desktop_name(), self._machine_id)
            )
        except websockets.exceptions.ConnectionClosed:
            # The phone never got its key; nobody could ever use this entry.
            self._forget(device.id, "could not remove an undelivered remote device")
            raise
        await websocket.close(1000)
        log.info(
            "pairing redeemed",
            extra={"extra_fields": {"device_id": device.id, "replaced": replaced}},
        )
        if replaced is not None:
            self._forget(replaced, "could not remove the replaced remote device")
            await self.close_device(replaced)
        if self._on_paired is not None:
            await self._on_paired(device)

    def _forget(self, device_id: str, failure: str) -> None:
        """Remove a device the pairing itself made stale; a failed write is logged."""
        try:
            self._devices.remove(device_id)
        except OSError:
            log.warning(failure, extra={"extra_fields": {"device_id": device_id}})

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
