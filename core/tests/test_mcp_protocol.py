"""TD-5136: MCP protocol upgrade.

Against the reference SDK's own servers (``mcp`` 2.0, a dev dependency),
for both protocol eras and both transports: tstd negotiates the version,
keeps working with a server that only speaks 2024-11-05, keeps an HTTP
call alive on progress (TD-5105), and follows ``tools/list_changed`` into
live sessions. Scripted HTTP servers pin the headers, the version
fallback, and the off-box opt-in.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.test_dispatch import make_classifier, make_config, wait_for_turn
from tests.test_mcp_tool_search import _call, _Endpoint, _sse
from tstd.config import (
    ConfigError,
    McpConfig,
    McpServerConfig,
    ModelConfig,
    ensure_user_config,
    load_config,
)
from tstd.config_write import save_mcp_server
from tstd.context.prompt import PromptAssembler
from tstd.loop import agent_loop
from tstd.mcp.era import LATEST_LEGACY, LATEST_MODERN
from tstd.mcp.errors import McpError
from tstd.mcp.http import HttpMcpClient
from tstd.mcp.loader import McpSupervisor
from tstd.mcp.urls import assert_http_url
from tstd.policy import ApprovalOutcome, PolicyConfig
from tstd.provider import ProviderClient
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.tools import ToolDispatcher, create_registry
from tstd.tools.handlers import register_builtin_handlers
from tstd.tools.registry import ToolRegistry

pytest.importorskip("mcp.server.mcpserver")

# The reference server. ``legacy`` patches the SDK's runner so the server
# speaks only 2024-11-05 and answers ``server/discover`` "Method not found",
# as a server built before 2026 does. ``http PORT`` serves Streamable HTTP.
_REFERENCE = r"""
import sys

args = sys.argv[1:]
if "legacy" in args:
    import mcp.server.runner as runner

    runner._has_modern_envelope = lambda params: False
    runner.HANDSHAKE_PROTOCOL_VERSIONS = ("2024-11-05",)
    runner.LATEST_HANDSHAKE_VERSION = "2024-11-05"
    import mcp.server.streamable_http_manager as manager

    class _Every:
        def __contains__(self, value):
            return True

    # Over HTTP a pre-2026 server routes every request to its session
    # transport, whatever MCP-Protocol-Version says.
    manager.HANDSHAKE_PROTOCOL_VERSIONS = _Every()

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

mcp = MCPServer("ref")


@mcp.tool()
async def echo(text: str) -> str:
    "Echo text back."
    return text


@mcp.tool()
async def slow(steps: int, ctx: Context) -> str:
    "Report progress every 0.3 s."
    for i in range(steps):
        await ctx.report_progress(i + 1, steps, f"step {i + 1}")
        await anyio.sleep(0.3)
    return "done"


@mcp.tool()
async def grow(ctx: Context) -> str:
    "Add a tool and announce it."

    async def extra() -> str:
        return "extra ran"

    mcp.add_tool(extra, name="extra", description="Added later.")
    if ctx.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS:
        await ctx.session.send_tool_list_changed()
    else:
        await ctx.notify_tools_changed()
    return "grown"


if "http" in args:
    mcp.run("streamable-http", port=int(args[args.index("http") + 1]))
else:
    mcp.run()
"""

ERAS = [("modern", LATEST_MODERN), ("legacy", "2024-11-05")]


def _script(tmp_path: Path) -> Path:
    path = tmp_path / "reference_server.py"
    path.write_text(_REFERENCE)
    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


async def _serve_http(script: Path, era: str) -> tuple[asyncio.subprocess.Process, str]:
    port = _free_port()
    argv = [sys.executable, str(script), *(["legacy"] if era == "legacy" else []), "http"]
    proc = await asyncio.create_subprocess_exec(
        *argv, str(port), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    for _ in range(300):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        return proc, f"http://127.0.0.1:{port}/mcp"
    proc.kill()
    raise AssertionError("reference HTTP server did not start")


@pytest.fixture
async def reference(
    tmp_path: Path, request: pytest.FixtureRequest
) -> AsyncIterator[Callable[[str, str], Awaitable[McpServerConfig]]]:
    """Builds a server config for (transport, era); stops what it started."""
    script = _script(tmp_path)
    procs: list[asyncio.subprocess.Process] = []

    async def make(transport: str, era: str) -> McpServerConfig:
        if transport == "stdio":
            extra = ["legacy"] if era == "legacy" else []
            return McpServerConfig(transport="stdio", command=[sys.executable, str(script), *extra])
        proc, url = await _serve_http(script, era)
        procs.append(proc)
        return McpServerConfig(transport="http", url=url)

    yield make
    for proc in procs:
        proc.terminate()
        await proc.wait()


async def _load(
    spec: McpServerConfig, *, call_timeout: float = 10.0
) -> tuple[McpSupervisor, ToolRegistry, ToolDispatcher]:
    supervisor = McpSupervisor(
        McpConfig(servers={"ref": spec}), handshake_timeout=15.0, call_timeout=call_timeout
    )
    registry = create_registry()
    dispatcher = ToolDispatcher(registry)
    try:
        await supervisor.ensure_loaded()
        supervisor.attach(registry, dispatcher)
    except BaseException:
        await supervisor.aclose()
        raise
    return supervisor, registry, dispatcher


async def _until(check: Callable[[], bool], seconds: float = 10.0) -> None:
    for _ in range(int(seconds / 0.05)):
        if check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


TRANSPORTS = ["stdio", "http"]


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize(("era", "version"), ERAS)
async def test_the_reference_server_loads_in_either_era(
    reference: Any, transport: str, era: str, version: str
) -> None:
    """The version is negotiated, shown on the doctor row, and calls work;
    a server that only speaks 2024-11-05 keeps working."""
    supervisor, registry, dispatcher = await _load(await reference(transport, era))
    try:
        [row] = supervisor.doctor_rows()
        assert row.status == "ok", row.detail
        assert row.detail.startswith(f"MCP {version}: 3 tool(s) listed"), row.detail
        assert {"ref__echo", "ref__slow", "ref__grow"} <= {t.name for t in registry.list_tools()}
        echo = dispatcher._handlers["ref__echo"]
        assert await echo(session=None, tool_call_id="t", text="hola") == "hola"
    finally:
        await supervisor.aclose()


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize(("era", "version"), ERAS)
async def test_progress_keeps_a_call_alive(
    reference: Any, transport: str, era: str, version: str
) -> None:
    """TD-5105 on both transports: a 1.5 s call with progress every 0.3 s
    outlives a 1 s idle timeout. Over HTTP, tstd used to send no
    progressToken, so the call timed out."""
    supervisor, _, dispatcher = await _load(await reference(transport, era), call_timeout=1.0)
    try:
        slow = dispatcher._handlers["ref__slow"]
        assert await slow(session=None, tool_call_id="t", steps=5) == "done"
    finally:
        await supervisor.aclose()


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize(("era", "version"), ERAS)
async def test_a_live_session_keeps_its_tools_after_a_reload(
    reference: Any, transport: str, era: str, version: str
) -> None:
    """A settings reload closes every client; a session that attached
    before it still calls its tools. Over HTTP the closed client used to
    keep its handshake and refuse with "not running"."""
    spec = await reference(transport, era)
    supervisor, _, dispatcher = await _load(spec)
    try:
        echo = dispatcher._handlers["ref__echo"]
        await supervisor.reload(McpConfig(servers={"ref": spec}))
        assert await echo(session=None, tool_call_id="t", text="again") == "again"
    finally:
        await supervisor.aclose()


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize(("era", "version"), ERAS)
async def test_tools_list_changed_reaches_a_live_session(
    reference: Any, transport: str, era: str, version: str
) -> None:
    supervisor, registry, dispatcher = await _load(await reference(transport, era))
    try:
        before = registry.version
        grow = dispatcher._handlers["ref__grow"]
        assert await grow(session=None, tool_call_id="t") == "grown"
        await _until(lambda: registry.get("ref__extra") is not None)
        assert registry.version > before
        extra = dispatcher._handlers["ref__extra"]
        assert await extra(session=None, tool_call_id="t") == "extra ran"
        await _until(lambda: "4 tool(s)" in supervisor.doctor_rows()[0].detail)
    finally:
        await supervisor.aclose()


async def test_the_model_is_offered_a_tool_the_server_added(tmp_path: Path) -> None:
    """The loop re-reads the registry when its version moves, so the next
    model call after ``tools/list_changed`` carries the new tool."""
    ws = tmp_path / "ws"
    ws.mkdir()
    spec = McpServerConfig(transport="stdio", command=[sys.executable, str(_script(tmp_path))])
    supervisor = McpSupervisor(
        McpConfig(servers={"ref": spec}), handshake_timeout=15.0, call_timeout=10.0
    )
    registry = create_registry()
    dispatcher = ToolDispatcher(registry, classifier=make_classifier(str(ws)), workspace=ws)
    dispatcher.policy = PolicyConfig()

    async def approve(*_args: Any) -> ApprovalOutcome:
        return ApprovalOutcome(True)

    dispatcher.approval_handler = approve
    register_builtin_handlers(dispatcher)
    endpoint = _Endpoint(
        [
            _call("ref__grow", {}, "call_1"),
            _sse({"role": "assistant", "content": "Grown."}, "stop"),
            _sse({"role": "assistant", "content": "Hi."}, "stop"),
        ]
    )
    config: ModelConfig = make_config()
    client = ProviderClient(
        base_url="http://127.0.0.1:9/v1",
        api_key=None,
        client=httpx.AsyncClient(transport=httpx.MockTransport(endpoint.handle)),
    )

    async def factory(*_args: object) -> ProviderClient:
        return client

    session = Session(str(ws))
    try:
        await supervisor.ensure_loaded()
        supervisor.attach(registry, dispatcher)
        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(
                s,
                TierRouter(),
                factory,
                config,
                tool_registry=registry,
                tool_dispatcher=dispatcher,
                prompt_assembler=PromptAssembler(ws, home_dir=tmp_path / "home"),
            ),
        )
        await runner.start()
        try:
            await session.add_user_message("grow")
            assert (await wait_for_turn(session, 1, 15.0)).failed is False
            await _until(lambda: registry.get("ref__extra") is not None)
            await session.add_user_message("again")
            assert (await wait_for_turn(session, 2, 15.0)).failed is False
        finally:
            await runner.cancel()
    finally:
        await supervisor.aclose()
        await client.close()
    assert "ref__extra" not in endpoint.tools(0)
    assert "ref__extra" in endpoint.tools(2)


# ── Scripted HTTP servers: headers, fallback, opt-in ────────────────────

Handler = Callable[[dict[str, Any], httpx.Request], httpx.Response]


def _scripted(handle: Handler) -> tuple[HttpMcpClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method != "POST":
            return httpx.Response(405)
        return handle(json.loads(request.content), request)

    client = HttpMcpClient("http://127.0.0.1:9/mcp", timeout=2.0)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    return client, seen


def _ok(body: dict[str, Any], result: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


def _error(body: dict[str, Any], code: int, data: Any = None) -> httpx.Response:
    error = {"code": code, "message": "no", "data": data}
    return httpx.Response(400, json={"jsonrpc": "2.0", "id": body["id"], "error": error})


_MARKED = {
    "name": "weather",
    "description": "Weather by region.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "region": {"type": "string", "x-mcp-header": "Region"},
            "days": {"type": "integer"},
        },
    },
}
_BAD_MARK = {
    "name": "broken",
    "description": "Marks a header name with a space.",
    "inputSchema": {
        "type": "object",
        "properties": {"q": {"type": "string", "x-mcp-header": "Bad Name"}},
    },
}


async def test_modern_requests_carry_the_protocol_headers() -> None:
    def handle(body: dict[str, Any], _request: httpx.Request) -> httpx.Response:
        if "id" not in body:
            return httpx.Response(202)
        method = body["method"]
        if method == "server/discover":
            return _ok(body, {"supportedVersions": [LATEST_MODERN], "capabilities": {}})
        if method == "tools/list":
            return _ok(body, {"tools": [_MARKED, _BAD_MARK]})
        # The answer as an SSE stream, after a progress notice.
        token = body["params"]["_meta"]["progressToken"]
        progress = {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {"progressToken": token, "progress": 1, "total": 1},
        }
        result = {"jsonrpc": "2.0", "id": body["id"], "result": {"content": []}}
        stream = f"data: {json.dumps(progress)}\n\ndata: {json.dumps(result)}\n\n"
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=stream.encode()
        )

    client, seen = _scripted(handle)
    updates: list[float] = []

    async def on_progress(progress: float, _total: float | None, _msg: str | None) -> None:
        updates.append(progress)

    try:
        await client.start()
        assert client.negotiated is not None and client.negotiated.era == "modern"
        assert [t.name for t in await client.list_tools()] == ["weather"]
        assert [e.name for e in client.excluded] == ["broken"]
        await client.call_tool("weather", {"region": "Zürich", "days": 2}, on_progress=on_progress)
    finally:
        await client.aclose()
    assert updates == [1]
    call = seen[-1]
    assert call.headers["accept"] == "application/json, text/event-stream"
    assert call.headers["mcp-protocol-version"] == LATEST_MODERN
    assert call.headers["mcp-method"] == "tools/call"
    assert call.headers["mcp-name"] == "weather"
    encoded = base64.b64encode("Zürich".encode()).decode()
    assert call.headers["mcp-param-region"] == f"=?base64?{encoded}?="
    meta = json.loads(call.content)["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == LATEST_MODERN
    assert meta["io.modelcontextprotocol/clientInfo"]["name"] == "tstd"
    assert "mcp-session-id" not in call.headers
    assert seen[0].headers["mcp-method"] == "server/discover"


async def test_a_legacy_only_version_list_falls_back_to_initialize() -> None:
    """-32022 naming only legacy versions: initialize, then the version
    header and the session id the server gave."""
    offered: list[str] = []

    def handle(body: dict[str, Any], _request: httpx.Request) -> httpx.Response:
        if "id" not in body:
            return httpx.Response(202)
        method = body["method"]
        if method == "server/discover":
            return _error(body, -32022, {"supported": ["2025-06-18"]})
        if method == "initialize":
            offered.append(body["params"]["protocolVersion"])
            response = _ok(body, {"protocolVersion": "2025-06-18", "capabilities": {}})
            response.headers["Mcp-Session-Id"] = "s-1"
            return response
        return _ok(body, {"tools": []})

    client, seen = _scripted(handle)
    try:
        await client.start()
        assert await client.list_tools() == []
    finally:
        await client.aclose()
    assert offered == [LATEST_LEGACY]
    listed = next(r for r in seen if r.method == "POST" and b"tools/list" in r.content)
    assert listed.headers["mcp-protocol-version"] == "2025-06-18"
    assert listed.headers["mcp-session-id"] == "s-1"
    assert "mcp-method" not in listed.headers
    assert any(r.method == "DELETE" and r.headers["mcp-session-id"] == "s-1" for r in seen)


async def test_a_server_with_no_shared_version_is_refused() -> None:
    def handle(body: dict[str, Any], _request: httpx.Request) -> httpx.Response:
        return _error(body, -32022, {"supported": ["2099-01-01"]})

    client, _ = _scripted(handle)
    try:
        with pytest.raises(McpError, match="2099-01-01"):
            await client.start()
    finally:
        await client.aclose()


async def test_a_2024_server_gets_no_version_header() -> None:
    def handle(body: dict[str, Any], _request: httpx.Request) -> httpx.Response:
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "server/discover":
            return httpx.Response(404)
        if body["method"] == "initialize":
            return _ok(body, {"protocolVersion": "2024-11-05", "capabilities": {}})
        return _ok(body, {"tools": []})

    client, seen = _scripted(handle)
    try:
        await client.start()
        await client.list_tools()
    finally:
        await client.aclose()
    listed = next(r for r in seen if b"tools/list" in r.content)
    assert "mcp-protocol-version" not in listed.headers


class TestOffBoxOptIn:
    def test_loopback_needs_no_opt_in(self) -> None:
        assert_http_url("http://127.0.0.1:8080/mcp")

    def test_off_box_is_refused_without_allow_remote(self) -> None:
        with pytest.raises(McpError, match="allow_remote"):
            assert_http_url("https://mcp.example.invalid/mcp")
        with pytest.raises(McpError, match="allow_remote"):
            HttpMcpClient("https://mcp.example.invalid/mcp")

    def test_off_box_must_be_https(self) -> None:
        with pytest.raises(McpError, match="https"):
            assert_http_url("http://mcp.example.invalid/mcp", allow_remote=True)

    def test_off_box_https_with_the_opt_in_is_accepted(self) -> None:
        assert_http_url("https://mcp.example.invalid/mcp", allow_remote=True)
        HttpMcpClient("https://mcp.example.invalid/mcp", allow_remote=True)

    def test_the_opt_in_is_per_server_and_off_by_default(self) -> None:
        spec = McpServerConfig(transport="http", url="https://mcp.example.invalid/mcp")
        assert spec.allow_remote is False
        on = McpServerConfig.model_validate(
            {"transport": "http", "url": "https://mcp.example.invalid/mcp", "allow_remote": True}
        )
        assert on.allow_remote is True

    async def test_the_supervisor_refuses_an_unopted_off_box_server(self) -> None:
        spec = McpServerConfig(transport="http", url="https://mcp.example.invalid/mcp")
        supervisor = McpSupervisor(McpConfig(servers={"far": spec}), handshake_timeout=1.0)
        try:
            [row] = await supervisor.ensure_loaded()
        finally:
            await supervisor.aclose()
        assert row.status == "fail" and "allow_remote" in row.detail

    def test_the_opt_in_is_written_and_read_back(self, tmp_path: Path) -> None:
        path = ensure_user_config(tmp_path / "config.yaml")
        url = "https://mcp.example.invalid/mcp"
        with pytest.raises(ConfigError, match="allow_remote"):
            save_mcp_server("far", McpServerConfig(transport="http", url=url), path)
        spec = McpServerConfig(transport="http", url=url, allow_remote=True)
        save_mcp_server("far", spec, path)
        assert "allow_remote: true" in path.read_text(encoding="utf-8")
        assert load_config(path).mcp.servers["far"].allow_remote is True
        plain = McpServerConfig(transport="http", url="http://mcp.example.invalid/mcp")
        with pytest.raises(ConfigError, match="https"):
            save_mcp_server("far", plain.model_copy(update={"allow_remote": True}), path)
