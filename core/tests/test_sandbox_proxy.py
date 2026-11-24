"""The sandbox's egress proxy (TD-5305, sandboxing R3).

Every test runs on this computer: the "allowed host" is a local test
server, reached through a connect seam from a public test address
(203.0.113.0/24, RFC 5737), so no outside host is contacted.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tstd.audit import AuditStore
from tstd.audit_writer import AuditWriter
from tstd.sandbox.proxy import EgressDecision, EgressProxy, listed, split_entry

ALLOWED = "203.0.113.5"
OWN = "198.51.100.7"
ANSWERS = {
    "allowed.test": [ALLOWED],
    "rebind.test": ["127.0.0.1"],
    "mapped.test": ["::ffff:127.0.0.1"],
    "linklocal.test": ["169.254.169.254"],
    "own.test": [OWN],
    "mixed.test": [ALLOWED, "127.0.0.1"],
}


class Rig:
    def __init__(self) -> None:
        self.decisions: list[EgressDecision] = []
        self.upstream_seen: list[bytes] = []
        self.upstream_port = 0
        self.dialled: list[tuple[str, int]] = []
        self.proxy = EgressProxy(
            self._record, resolve=self._resolve, connect=self._connect, own={OWN}
        )
        self.proxy_port = 0

    async def _record(self, decision: EgressDecision) -> None:
        self.decisions.append(decision)

    async def _resolve(self, host: str, port: int) -> list[str]:
        if host not in ANSWERS:
            raise OSError("no such name")
        return ANSWERS[host]

    async def _connect(self, address: str, port: int) -> Any:
        self.dialled.append((address, port))
        assert address == ALLOWED, "the proxy dialled an address it should have refused"
        return await asyncio.open_connection("127.0.0.1", self.upstream_port)

    async def _upstream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(65536)
        self.upstream_seen.append(data)
        if data.startswith(b"GET "):
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
        else:
            writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()

    def grant(self, *hosts: str) -> str:
        return self.proxy.grant("s1", "call-1", hosts).token

    async def ask(self, head: str, token: str | None, body: bytes = b"") -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy_port)
        auth = ""
        if token is not None:
            cred = base64.b64encode(f"tst:{token}".encode()).decode()
            auth = f"Proxy-Authorization: Basic {cred}\r\n"
        first, _, rest = head.partition("\r\n")
        writer.write(f"{first}\r\n{auth}{rest}".encode() + body)
        await writer.drain()
        out = await asyncio.wait_for(
            reader.read(-1) if not body else self._after(reader, writer, body), 10
        )
        writer.close()
        return out

    async def _after(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, body: bytes
    ) -> bytes:
        established = await reader.readuntil(b"\r\n\r\n")
        if b" 200 " not in established:
            return established + await reader.read(-1)
        writer.write(body)
        await writer.drain()
        writer.write_eof()
        return established + await reader.read(-1)


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    r = Rig()
    upstream = await asyncio.start_server(r._upstream, "127.0.0.1", 0)
    r.upstream_port = upstream.sockets[0].getsockname()[1]
    proxy = await asyncio.start_server(r.proxy.serve, "127.0.0.1", 0)
    r.proxy_port = proxy.sockets[0].getsockname()[1]
    try:
        yield r
    finally:
        proxy.close()
        upstream.close()


def _status(reply: bytes) -> int:
    return int(reply.split(b" ", 2)[1])


async def test_connect_to_a_listed_host_relays(rig: Rig) -> None:
    token = rig.grant(f"allowed.test:{8443}")
    reply = await rig.ask(
        "CONNECT allowed.test:8443 HTTP/1.1\r\nHost: allowed.test\r\n\r\n", token, b"ping"
    )
    assert reply.startswith(b"HTTP/1.1 200 ")
    assert reply.endswith(b"echo:ping")
    assert rig.dialled == [(ALLOWED, 8443)]
    [d] = rig.decisions
    assert (d.verdict, d.host, d.port, d.address, d.tool_call_id) == (
        "allow",
        "allowed.test",
        8443,
        ALLOWED,
        "call-1",
    )


async def test_plain_http_goes_upstream_without_the_token_and_closes(rig: Rig) -> None:
    token = rig.grant("allowed.test")
    reply = await rig.ask(
        "GET http://allowed.test/path?q=1 HTTP/1.1\r\nHost: allowed.test\r\n"
        "Connection: keep-alive\r\n\r\n",
        token,
    )
    assert reply.endswith(b"hi")
    [sent] = rig.upstream_seen
    assert sent.startswith(b"GET /path?q=1 HTTP/1.1\r\n")
    assert b"Proxy-Authorization" not in sent and token.encode() not in sent
    assert b"Connection: close" in sent and b"keep-alive" not in sent
    assert rig.dialled == [(ALLOWED, 80)]


@pytest.mark.parametrize(
    ("target", "listed_as", "why"),
    [
        ("elsewhere.test:443", "allowed.test", "not on this workspace's network allowlist"),
        ("allowed.test:8080", "allowed.test", "allowed.test:8080 is not on"),
        ("203.0.113.9:443", "allowed.test", "not on this workspace's network allowlist"),
        ("127.0.0.1:443", "127.0.0.1", "loopback"),
        ("[::1]:443", "::1", "loopback"),
        ("rebind.test:443", "rebind.test", "loopback"),
        ("mapped.test:443", "mapped.test", "loopback"),
        ("linklocal.test:443", "linklocal.test", "link-local"),
        ("own.test:443", "own.test", "this computer's own addresses"),
        ("mixed.test:443", "mixed.test", "loopback"),
        ("nowhere.test:443", "nowhere.test", "did not resolve"),
    ],
)
async def test_refused_destinations(rig: Rig, target: str, listed_as: str, why: str) -> None:
    token = rig.grant(listed_as)
    reply = await rig.ask(f"CONNECT {target} HTTP/1.1\r\n\r\n", token)
    assert _status(reply) in (403, 502)
    assert why in reply.decode()
    assert rig.dialled == []
    [d] = rig.decisions
    assert d.verdict == "deny" and why in d.reason and d.tool_call_id == "call-1"


async def test_a_listed_public_literal_connects(rig: Rig) -> None:
    token = rig.grant(f"{ALLOWED}:9000")
    reply = await rig.ask(f"CONNECT {ALLOWED}:9000 HTTP/1.1\r\n\r\n", token, b"x")
    assert reply.endswith(b"echo:x")


@pytest.mark.parametrize("token", [None, "wrong", ""])
async def test_no_token_or_a_wrong_one_is_407_and_dials_nothing(
    rig: Rig, token: str | None
) -> None:
    rig.grant("allowed.test")
    reply = await rig.ask("CONNECT allowed.test:443 HTTP/1.1\r\n\r\n", token)
    assert _status(reply) == 407
    assert b'Proxy-Authenticate: Basic realm="tst-sandbox"' in reply
    assert rig.dialled == [] and rig.decisions == []


async def test_a_revoked_grant_is_407(rig: Rig) -> None:
    token = rig.grant("allowed.test")
    rig.proxy.revoke(token)
    assert rig.proxy.live_grants() == 0
    reply = await rig.ask("CONNECT allowed.test:443 HTTP/1.1\r\n\r\n", token)
    assert _status(reply) == 407


async def test_one_commands_token_is_not_anothers(rig: Rig) -> None:
    rig.proxy.grant("s1", "call-a", ["allowed.test"])
    other = rig.proxy.grant("s1", "call-b", ["elsewhere.test"]).token
    reply = await rig.ask("CONNECT allowed.test:443 HTTP/1.1\r\n\r\n", other)
    assert _status(reply) == 403
    assert rig.decisions[0].tool_call_id == "call-b"


@pytest.mark.parametrize(
    "head",
    [
        "GET https://allowed.test/ HTTP/1.1\r\n\r\n",
        "GET /relative HTTP/1.1\r\nHost: allowed.test\r\n\r\n",
        "CONNECT allowed.test HTTP/1.1\r\n\r\n",
        "NONSENSE\r\n\r\n",
    ],
)
async def test_what_is_not_a_proxy_request_is_400(rig: Rig, head: str) -> None:
    token = rig.grant("allowed.test")
    reply = await rig.ask(head, token)
    assert _status(reply) == 400
    assert rig.dialled == []


async def test_an_audit_failure_does_not_open_or_hang_anything(rig: Rig) -> None:
    async def broken(_d: EgressDecision) -> None:
        raise RuntimeError("disk full")

    rig.proxy._on_decision = broken
    token = rig.grant("allowed.test")
    reply = await rig.ask("CONNECT elsewhere.test:443 HTTP/1.1\r\n\r\n", token)
    assert _status(reply) == 403


def test_list_entries() -> None:
    assert split_entry("Example.COM.") == ("example.com", None)
    assert split_entry("example.com:8443") == ("example.com", 8443)
    assert split_entry("[::1]:443") == ("::1", 443)
    assert split_entry("::1") == ("::1", None)
    assert listed("example.com", 443, ["example.com"])
    assert listed("example.com", 80, ["example.com"])
    assert not listed("example.com", 22, ["example.com"])
    assert listed("example.com", 22, ["example.com:22"])
    assert not listed("sub.example.com", 443, ["example.com"])


async def test_decisions_reach_the_audit_log_with_the_tool_call(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "audit.db")
    store.append_session("s1", str(tmp_path), 1.0)
    writer = AuditWriter(store)
    writer.start()
    writer.record_network_egress(
        EgressDecision("s1", "call-9", "elsewhere.test", 443, "deny", "not listed")
    )
    writer.record_network_egress(
        EgressDecision("s1", "call-9", "allowed.test", 443, "allow", "on the allowlist", ALLOWED)
    )
    await writer.close()
    check = AuditStore(tmp_path / "audit.db")
    rows = check._conn.execute(
        "SELECT tool_call_id, host, port, address, verdict, reason FROM network_egress ORDER BY id"
    ).fetchall()
    check.close()
    assert rows == [
        ("call-9", "elsewhere.test", 443, "", "deny", "not listed"),
        ("call-9", "allowed.test", 443, ALLOWED, "allow", "on the allowlist"),
    ]
