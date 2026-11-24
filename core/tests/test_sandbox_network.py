"""A sandboxed command reaches boundary.network's hosts, and nothing else (TD-5306).

Real bubblewrap runs (``needs_bwrap``; CI sets ``TST_REQUIRE_SANDBOX``).
The "allowed host" is a server on this computer, reached through the
proxy's connect seam from an RFC 5737 test address, so no outside host is
contacted. Inside the sandbox the command uses this interpreter's urllib,
which honours the proxy variables with their credentials.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_shell_sandbox import needs_bwrap
from tstd import shell_sandbox
from tstd.boundary_config import BoundaryConfig, BoundarySection
from tstd.config import SandboxDefaults
from tstd.sandbox import egress
from tstd.sandbox.proxy import EgressDecision, EgressProxy
from tstd.session import Session
from tstd.shell_sandbox import SandboxSection, build_plan, effective
from tstd.tools import run_shell
from tstd.workspace_trust import find_loosened

ALLOWED = "203.0.113.5"
PY = sys.executable


class Upstream:
    def __init__(self) -> None:
        self.port = 0
        self.decisions: list[EgressDecision] = []
        self.proxy = EgressProxy(self._record, resolve=self._resolve, connect=self._connect, own=())

    async def _record(self, decision: EgressDecision) -> None:
        self.decisions.append(decision)

    async def _resolve(self, host: str, port: int) -> list[str]:
        return [ALLOWED] if host == "allowed.test" else ["198.51.100.20"]

    async def _connect(self, address: str, port: int) -> Any:
        assert address == ALLOWED
        return await asyncio.open_connection("127.0.0.1", self.port)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\nfrom-outside")
        await writer.drain()
        writer.close()


@pytest.fixture
async def upstream() -> AsyncIterator[Upstream]:
    up = Upstream()
    server = await asyncio.start_server(up.serve, "127.0.0.1", 0)
    up.port = server.sockets[0].getsockname()[1]
    egress.set_proxy(up.proxy)
    try:
        yield up
    finally:
        egress.set_proxy(None)
        server.close()


def _session(ws: Path, hosts: list[str], network: str | None = "allowlist") -> Session:
    session = Session(str(ws))
    session.boundary_config = BoundaryConfig(
        boundary=BoundarySection(network=hosts or "deny"),
        sandbox=SandboxSection(enabled=True, network=network),  # type: ignore[arg-type]
    )
    return session


def _fetch(url: str) -> str:
    code = (
        "import urllib.request, urllib.error\n"
        "try:\n"
        f"    print('BODY', urllib.request.urlopen({url!r}, timeout=10).read().decode())\n"
        "except urllib.error.HTTPError as e:\n"
        "    print('STATUS', e.code, e.read().decode().strip())\n"
        "except OSError as e:\n"
        "    print('OSERROR', e)\n"
    )
    return f"{PY} -c {_quote(code)}"


def _quote(text: str) -> str:
    import shlex

    return shlex.quote(text)


@needs_bwrap
class TestThroughTheSandbox:
    async def test_a_listed_host_is_reached_and_audited_with_the_call(
        self, tmp_path: Path, upstream: Upstream
    ) -> None:
        session = _session(tmp_path, [f"allowed.test:{upstream.port}"])
        out = await run_shell(
            session, _fetch(f"http://allowed.test:{upstream.port}/x"), tool_call_id="call-7"
        )
        assert "BODY from-outside" in out, out
        assert "network only to the hosts on boundary.network" in out
        [d] = upstream.decisions
        assert (d.verdict, d.host, d.tool_call_id, d.session_id) == (
            "allow",
            "allowed.test",
            "call-7",
            session.id,
        )

    async def test_an_unlisted_host_is_403_with_the_reason(
        self, tmp_path: Path, upstream: Upstream
    ) -> None:
        session = _session(tmp_path, [f"allowed.test:{upstream.port}"])
        out = await run_shell(session, _fetch("http://elsewhere.test/"), tool_call_id="call-8")
        assert "STATUS 403" in out and "boundary.network" in out, out
        assert [d.verdict for d in upstream.decisions] == ["deny"]

    async def test_without_the_proxy_variables_there_is_no_route_out(
        self, tmp_path: Path, upstream: Upstream
    ) -> None:
        """The owner's condition: the namespace, not the variables, is the boundary."""
        session = _session(tmp_path, [f"allowed.test:{upstream.port}"])
        code = (
            "import os, socket\n"
            "for k in list(os.environ):\n"
            "    if 'proxy' in k.lower(): del os.environ[k]\n"
            f"for host, port in (({ALLOWED!r}, {upstream.port}), ('127.0.0.1', {upstream.port}),"
            " ('1.1.1.1', 443)):\n"
            "    try:\n"
            "        socket.create_connection((host, port), timeout=3)\n"
            "        print('CONNECTED', host)\n"
            "    except OSError as e:\n"
            "        print('NO ROUTE', host, e.errno)\n"
        )
        out = await run_shell(session, f"{PY} -c {_quote(code)}")
        assert "CONNECTED" not in out, out
        assert out.count("NO ROUTE") == 3, out
        assert upstream.decisions == []

    async def test_the_grant_ends_with_the_command(
        self, tmp_path: Path, upstream: Upstream
    ) -> None:
        session = _session(tmp_path, [f"allowed.test:{upstream.port}"])
        await run_shell(session, "true")
        for _ in range(50):
            if upstream.proxy.live_grants() == 0:
                break
            await asyncio.sleep(0.05)
        assert upstream.proxy.live_grants() == 0

    async def test_deny_by_default_sets_no_proxy_and_grants_nothing(
        self, tmp_path: Path, upstream: Upstream
    ) -> None:
        session = _session(tmp_path, [f"allowed.test:{upstream.port}"], network=None)
        out = await run_shell(session, "env | grep -c 127.0.0.1:3128 || true")
        assert "── stdout ──\n0\n" in out, out
        assert upstream.proxy.live_grants() == 0
        assert "no network or host sockets" in out

    async def test_no_hosts_listed_means_no_network(
        self, tmp_path: Path, upstream: Upstream
    ) -> None:
        session = _session(tmp_path, [])
        out = await run_shell(session, _fetch(f"http://allowed.test:{upstream.port}/x"))
        assert "OSERROR" in out, out
        assert upstream.decisions == []


def test_the_bridge_runs_first_and_execs_the_command(tmp_path: Path) -> None:
    plan = build_plan(
        str(tmp_path),
        "echo hi",
        SandboxSection(enabled=True),
        seccomp=tmp_path / "f",
        bridge=["py", "-c", "bridge", "7", "3128"],
        pass_fds=(7,),
    )
    tail = plan.argv[plan.argv.index("--") + 1 :]
    assert tail == ["py", "-c", "bridge", "7", "3128", "/bin/sh", "-c", "echo hi"]
    assert plan.pass_fds == (7,)
    assert "--unshare-all" in plan.argv


def test_the_setting_follows_the_user_default_and_the_workspace_wins() -> None:
    assert effective(SandboxSection(), SandboxDefaults()).network == "deny"
    allow = SandboxDefaults(network="allowlist")
    assert effective(SandboxSection(), allow).network == "allowlist"
    assert effective(SandboxSection(network="deny"), allow).network == "deny"
    assert not shell_sandbox.network_allowed(SandboxSection())


def test_a_workspace_allowlist_waits_for_trust(tmp_path: Path) -> None:
    data = {"sandbox": {"enabled": True, "network": "allowlist"}}
    found = find_loosened(tmp_path / ".tst" / "config.yaml", data)
    assert any(item.key == "sandbox.network" for item in found), found
