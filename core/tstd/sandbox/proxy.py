"""The sandbox's way out: an allow-listing HTTP proxy (TD-5305, sandboxing R3).

A sandboxed shell command has no network: every namespace is unshared.
With ``sandbox.network: allowlist`` (TD-5306) it gets one way out, this
proxy, and only to the hosts on ``boundary.network``. The proxy is the
check; the namespace with no other route is the boundary.

- ``CONNECT host:port`` (HTTPS, git over https) and plain ``http://`` URLs.
  No SOCKS, no TLS interception: the proxy sees the host and port, never
  the traffic.
- A host must be on the grant's list. A bare name allows ports 80 and
  443; ``host:port`` allows that port. An IP literal is refused unless it
  is listed itself.
- The proxy resolves the name itself and connects to the address it
  checked, so an answer cannot change between the check and the dial. An
  answer that is loopback, link-local, unspecified, multicast or one of
  this computer's own addresses is refused, so a listed name cannot be
  pointed at the daemon or another local service.
- Each command has its own token (``ProxyGrant``); a request without a
  known one is answered 407. That keeps another local process, or another
  command, from borrowing a command's grant.
- Every allow and deny is reported to ``on_decision``, which the daemon
  writes to the audit log with the tool call id.

This module holds no listener. The daemon hands it one connection at a
time (``serve``): on Linux over a socket pair from inside the sandbox, so
nothing on the host listens at all.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import ipaddress
import secrets
import socket
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from ..logging import get_logger

log = get_logger("tstd.sandbox.proxy")

Verdict = Literal["allow", "deny"]

#: The longest request head the proxy reads before it answers 400.
HEAD_LIMIT = 16 * 1024
#: Seconds to read a request head, to resolve, and to connect.
HEAD_TIMEOUT = 15.0
CONNECT_TIMEOUT = 15.0
#: Ports a bare host entry allows.
WEB_PORTS = frozenset({80, 443})
_RELAY_CHUNK = 64 * 1024


@dataclass(frozen=True)
class EgressDecision:
    """One allow or deny, for the audit log."""

    session_id: str
    tool_call_id: str
    host: str
    port: int
    verdict: Verdict
    reason: str
    address: str = ""
    ts: float = field(default_factory=time.time)


OnDecision = Callable[[EgressDecision], Awaitable[None]]
Resolve = Callable[[str, int], Awaitable[list[str]]]
Connect = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


@dataclass(frozen=True)
class ProxyGrant:
    """What one sandboxed command may reach, and the token that proves it is that command."""

    token: str
    session_id: str
    tool_call_id: str
    hosts: frozenset[str]


class Refused(Exception):
    """A request the proxy answers itself: a status and a one-line reason."""

    def __init__(self, status: int, reason: str, *, host: str = "", port: int = 0) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.host = host
        self.port = port


def normalize_host(host: str) -> str:
    """Lower case, no brackets, no trailing dot."""
    host = host.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.rstrip(".")


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def listed(host: str, port: int, hosts: Collection[str]) -> bool:
    """Whether ``host:port`` is on the list: a bare entry is the web ports only."""
    name = normalize_host(host)
    for entry in hosts:
        entry_host, entry_port = split_entry(entry)
        if entry_host != name:
            continue
        if entry_port is None and port in WEB_PORTS:
            return True
        if entry_port == port:
            return True
    return False


def split_entry(entry: str) -> tuple[str, int | None]:
    """``host`` or ``host:port`` (``[v6]:port`` for an IPv6 literal)."""
    text = entry.strip()
    if text.startswith("["):
        inside, _, rest = text[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
        return normalize_host(inside), int(port) if port.isdigit() else None
    if text.count(":") == 1:
        name, _, port = text.partition(":")
        if port.isdigit():
            return normalize_host(name), int(port)
    return normalize_host(text), None


def refused_address(address: str, own: Collection[str]) -> str | None:
    """Why the proxy will not connect to *address*, or None."""
    ip = _ip(address)
    if ip is None:
        return "not an address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "it resolves to this computer (loopback)"
    if ip.is_link_local:
        return "it resolves to a link-local address"
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return "it resolves to an address that is not a host"
    if str(ip) in own:
        return "it resolves to one of this computer's own addresses"
    return None


def own_addresses() -> frozenset[str]:
    """This computer's own addresses, as far as its name resolves."""
    found: set[str] = set()
    for name in {socket.gethostname(), socket.getfqdn()}:
        with contextlib.suppress(OSError):
            for info in socket.getaddrinfo(name, None):
                found.add(str(info[4][0]).split("%")[0])
    return frozenset(found)


async def _resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0]).split("%")[0]
        if address not in seen:
            seen.append(address)
    return seen


class EgressProxy:
    """The grants, the checks, and one connection at a time."""

    def __init__(
        self,
        on_decision: OnDecision,
        *,
        resolve: Resolve = _resolve,
        connect: Connect = asyncio.open_connection,
        own: Collection[str] | None = None,
    ) -> None:
        self._on_decision = on_decision
        self._resolve = resolve
        self._connect = connect
        self._own = frozenset(own) if own is not None else own_addresses()
        self._grants: dict[str, ProxyGrant] = {}

    # ── grants ─────────────────────────────────────────────────────────

    def grant(self, session_id: str, tool_call_id: str, hosts: Collection[str]) -> ProxyGrant:
        """A new token for one command. Revoke it when the command is reaped."""
        token = secrets.token_urlsafe(24)
        grant = ProxyGrant(token, session_id, tool_call_id, frozenset(hosts))
        self._grants[token] = grant
        return grant

    def revoke(self, token: str) -> None:
        self._grants.pop(token, None)

    def live_grants(self) -> int:
        return len(self._grants)

    # ── one connection ────────────────────────────────────────────────

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Answer one client connection, then close it. Never raises."""
        try:
            await self._serve(reader, writer)
        except Exception as exc:  # the connection is the client's; the daemon carries on
            log.warning(
                "egress proxy connection failed", extra={"extra_fields": {"error": str(exc)}}
            )
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            await _answer(writer, 400, "The request head was incomplete or too long.")
            return
        if len(head) > HEAD_LIMIT:
            await _answer(writer, 400, "The request head was too long.")
            return
        try:
            method, target, version, headers = _parse_head(head)
        except ValueError as exc:
            await _answer(writer, 400, str(exc))
            return
        grant = self._grants.get(_token(headers) or "")
        if grant is None:
            await _answer(writer, 407, "This proxy needs the command's own token.", auth=True)
            return
        try:
            host, port, upstream_head = _destination(method, target, version, headers)
            address = await self._check(grant, host, port)
        except Refused as refused:
            await self._decide(grant, refused.host, refused.port, "deny", refused.reason)
            await _answer(writer, refused.status, refused.reason)
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                self._connect(address, port), CONNECT_TIMEOUT
            )
        except (OSError, TimeoutError) as exc:
            reason = f"Could not connect to {host}:{port}: {exc.__class__.__name__}"
            await self._decide(grant, host, port, "deny", reason, address)
            await _answer(writer, 502, reason)
            return
        await self._decide(grant, host, port, "allow", "on the allowlist", address)
        if upstream_head is None:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
        else:
            up_writer.write(upstream_head)
            await up_writer.drain()
        await _relay(reader, writer, up_reader, up_writer)

    async def _check(self, grant: ProxyGrant, host: str, port: int) -> str:
        """The address to dial for *host*, or ``Refused``."""
        name = normalize_host(host)
        if not 0 < port < 65536:
            raise Refused(400, f"Port {port} is not a port.", host=name, port=port)
        if not listed(name, port, grant.hosts):
            where = name if port in WEB_PORTS else f"{name}:{port}"
            raise Refused(
                403,
                f"{where} is not on this workspace's network allowlist (boundary.network).",
                host=name,
                port=port,
            )
        literal = _ip(name)
        if literal is not None:
            why = refused_address(name, self._own)
            if why is not None:
                raise Refused(403, f"{name} is refused: {why}.", host=name, port=port)
            return name
        try:
            answers = await asyncio.wait_for(self._resolve(name, port), CONNECT_TIMEOUT)
        except (OSError, TimeoutError) as exc:
            raise Refused(502, f"{name} did not resolve: {exc}", host=name, port=port) from exc
        if not answers:
            raise Refused(502, f"{name} did not resolve.", host=name, port=port)
        for answer in answers:
            why = refused_address(answer, self._own)
            if why is not None:
                raise Refused(403, f"{name} is refused: {why}.", host=name, port=port)
        return answers[0]

    async def _decide(
        self,
        grant: ProxyGrant,
        host: str,
        port: int,
        verdict: Verdict,
        reason: str,
        address: str = "",
    ) -> None:
        decision = EgressDecision(
            session_id=grant.session_id,
            tool_call_id=grant.tool_call_id,
            host=host,
            port=port,
            verdict=verdict,
            reason=reason,
            address=address,
        )
        try:
            await self._on_decision(decision)
        except Exception as exc:  # an audit failure must not open or hang the connection
            fields = {"error": str(exc)}
            log.warning("egress decision not recorded", extra={"extra_fields": fields})


# ── HTTP plumbing ────────────────────────────────────────────────────────

Headers = list[tuple[str, str]]
_HOP_BY_HOP = frozenset(
    {
        "proxy-authorization",
        "proxy-connection",
        "proxy-authenticate",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
    }
)


def _parse_head(head: bytes) -> tuple[str, str, str, Headers]:
    try:
        text = head.decode("latin-1")
    except UnicodeDecodeError as exc:  # latin-1 decodes every byte; kept for clarity
        raise ValueError("The request head is not text.") from exc
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise ValueError("The request line is not HTTP/1.x.")
    headers: Headers = []
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            raise ValueError("A request header is malformed.")
        headers.append((name, value.strip()))
    return parts[0].upper(), parts[1], parts[2], headers


def _header(headers: Headers, name: str) -> str | None:
    wanted = name.lower()
    return next((value for key, value in headers if key.lower() == wanted), None)


def _token(headers: Headers) -> str | None:
    """The password of a Basic ``Proxy-Authorization``; the user name is ignored."""
    value = _header(headers, "Proxy-Authorization")
    if value is None:
        return None
    scheme, _, encoded = value.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    _, sep, password = decoded.partition(":")
    return password if sep else None


def _destination(
    method: str, target: str, version: str, headers: Headers
) -> tuple[str, int, bytes | None]:
    """Host, port and, for plain HTTP, the head to send upstream."""
    if method == "CONNECT":
        host, port = split_entry(target)
        if port is None:
            raise Refused(400, "CONNECT needs host:port.")
        return host, port, None
    url = urlsplit(target)
    if url.scheme != "http" or not url.hostname:
        raise Refused(400, "Only http:// URLs and CONNECT go through this proxy.", host="", port=0)
    try:
        port = url.port or 80
    except ValueError as exc:
        raise Refused(400, "The URL's port is not a number.") from exc
    path = url.path or "/"
    if url.query:
        path += f"?{url.query}"
    kept = [(k, v) for k, v in headers if k.lower() not in _HOP_BY_HOP]
    if _header(kept, "Host") is None:
        kept.insert(0, ("Host", url.netloc.rpartition("@")[2]))
    # One request per connection: a second one on a kept-alive connection
    # could name another host and would not be checked.
    kept.append(("Connection", "close"))
    lines = [f"{method} {path} {version}", *(f"{k}: {v}" for k, v in kept), "", ""]
    return normalize_host(url.hostname), port, "\r\n".join(lines).encode("latin-1")


async def _answer(
    writer: asyncio.StreamWriter, status: int, reason: str, *, auth: bool = False
) -> None:
    phrase = {
        400: "Bad Request",
        403: "Forbidden",
        407: "Proxy Authentication Required",
        502: "Bad Gateway",
    }.get(status, "Error")
    body = (reason + "\n").encode("utf-8")
    lines = [
        f"HTTP/1.1 {status} {phrase}",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    if auth:
        lines.append('Proxy-Authenticate: Basic realm="tst-sandbox"')
    with contextlib.suppress(ConnectionError):
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
        await writer.drain()


async def _relay(
    a_reader: asyncio.StreamReader,
    a_writer: asyncio.StreamWriter,
    b_reader: asyncio.StreamReader,
    b_writer: asyncio.StreamWriter,
) -> None:
    async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError, asyncio.IncompleteReadError):
            while chunk := await src.read(_RELAY_CHUNK):
                dst.write(chunk)
                await dst.drain()
        with contextlib.suppress(Exception):
            if dst.can_write_eof():
                dst.write_eof()

    try:
        await asyncio.gather(pump(a_reader, b_writer), pump(b_reader, a_writer))
    finally:
        with contextlib.suppress(Exception):
            b_writer.close()
            await b_writer.wait_closed()
