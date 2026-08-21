"""Tests for MCP server loading (TD-4401): manager, transports, registration."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from tstd.config import McpConfig, McpServerConfig
from tstd.mcp import McpError, McpManager, register_mcp_tools
from tstd.mcp.manager import HttpTransport, StdioTransport, _content_to_text
from tstd.tools.dispatch import ToolDispatcher
from tstd.tools.registry import ToolRegistry

from .fake_mcp_server import spec as fake_spec

ECHO_TOOLS = [
    {
        "name": "echo",
        "description": "Echo the arguments back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
        },
    },
    {
        "name": "weird",
        "description": "",
        "inputSchema": {"type": "string"},  # not an object schema
    },
]


# ── Stdio transport ────────────────────────────────────────────────────


class TestStdioTransport:
    @pytest.mark.asyncio
    async def test_lists_and_calls_tools(self) -> None:
        transport = StdioTransport(fake_spec(ECHO_TOOLS))
        try:
            await transport.start()
            tools = await transport.list_tools()
            assert [t["name"] for t in tools] == ["echo", "weird"]
            result = await transport.call_tool("echo", {"text": "hi"})
            blocks = [b["text"] for b in result["content"] if b.get("type") == "text"]
            assert blocks[0] == "echo done"
            assert json.loads(blocks[1]) == {"text": "hi"}
            assert transport.is_alive()
        finally:
            await transport.aclose()

    @pytest.mark.asyncio
    async def test_json_rpc_error_maps_to_mcp_error(self) -> None:
        # A command that exits immediately: initialize reads EOF and the
        # failure surfaces as a transport error carrying MCP wording,
        # never a computer-use code.
        transport = StdioTransport([sys.executable, "-c", "raise SystemExit(1)"])
        with pytest.raises(McpError):
            await transport.start()


# ── HTTP transport ─────────────────────────────────────────────────────


class _JsonRpcHandler(BaseHTTPRequestHandler):
    """Minimal loopback MCP-over-HTTP server: JSON responses only."""

    server_version = "FakeMCP/0"

    def log_message(self, *args: Any) -> None:  # silence test output
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        message = json.loads(self.rfile.read(length))
        method = message.get("method")
        mid = message.get("id")

        def reply(result: dict[str, Any]) -> None:
            body = json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if method == "initialize":
                self.send_header("Mcp-Session-Id", "sess-123")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        if method == "initialize":
            reply({"protocolVersion": "2024-11-05", "capabilities": {}})
        elif method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif method == "tools/list":
            reply({"tools": ECHO_TOOLS[:1]})
        elif method == "tools/call":
            name = (message.get("params") or {}).get("name")
            reply({"content": [{"type": "text", "text": f"http {name} done"}]})
        else:
            reply({})


class TestHttpTransport:
    @pytest.mark.asyncio
    async def test_initialize_list_and_call_with_session_id(self) -> None:
        httpd = HTTPServer(("127.0.0.1", 0), _JsonRpcHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{httpd.server_port}/mcp"
        transport = HttpTransport(url)
        try:
            await transport.start()
            tools = await transport.list_tools()
            assert [t["name"] for t in tools] == ["echo"]
            text = _content_to_text(await transport.call_tool("echo", {}), "srv")
            assert text == "http echo done"
        finally:
            await transport.aclose()
            httpd.shutdown()

    @pytest.mark.asyncio
    async def test_event_stream_response_is_refused_not_half_read(self) -> None:
        class SseHandler(_JsonRpcHandler):
            def do_POST(self) -> None:
                body = b"event: message\ndata: {}\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = HTTPServer(("127.0.0.1", 0), SseHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        transport = HttpTransport(f"http://127.0.0.1:{httpd.server_port}/mcp")
        try:
            with pytest.raises(McpError, match="event stream"):
                await transport.start()
        finally:
            await transport.aclose()
            httpd.shutdown()


# ── Manager lifecycle ──────────────────────────────────────────────────


def _config(**servers: McpServerConfig) -> McpConfig:
    return McpConfig(servers=dict(servers))


class TestManager:
    @pytest.mark.asyncio
    async def test_ready_server_contributes_tools(self) -> None:
        manager = McpManager(_config(git=McpServerConfig(command=fake_spec(ECHO_TOOLS))))
        await manager.ensure_started()
        tools = manager.tools()
        assert [(t.server, t.name) for t in tools] == [("git", "echo"), ("git", "weird")]
        statuses = manager.statuses()
        assert statuses[0].status == "ready" and statuses[0].tool_count == 2
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_dead_server_is_recorded_never_raised(self) -> None:
        manager = McpManager(
            _config(
                dead=McpServerConfig(command=[sys.executable, "-c", "raise SystemExit(1)"]),
                gone=McpServerConfig(command=["tstd-definitely-not-a-binary"]),
            )
        )
        await manager.ensure_started()  # must not raise
        by_name = {s.name: s for s in manager.statuses()}
        assert by_name["dead"].status == "failed" and by_name["dead"].detail
        assert by_name["gone"].status == "failed"
        assert manager.tools() == []
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_disabled_server_never_starts(self) -> None:
        manager = McpManager(_config(off=McpServerConfig(command=fake_spec(), enabled=False)))
        await manager.ensure_started()
        statuses = manager.statuses()
        assert statuses[0].status == "disabled"
        assert manager.tools() == []
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_ensure_started_is_idempotent(self) -> None:
        manager = McpManager(_config(git=McpServerConfig(command=fake_spec(ECHO_TOOLS))))
        await manager.ensure_started()
        await manager.ensure_started()
        # One process, one catalog: a second start would have replaced the
        # client; the tool list staying stable is the observable.
        assert len(manager.tools()) == 2
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_starting_status_before_start(self) -> None:
        manager = McpManager(_config(git=McpServerConfig(command=fake_spec())))
        assert manager.statuses()[0].status == "starting"
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_empty_config_reports_nothing(self) -> None:
        manager = McpManager(McpConfig())
        assert not manager.has_servers()
        await manager.ensure_started()
        assert manager.statuses() == []
        await manager.aclose()


class TestCall:
    @pytest.mark.asyncio
    async def test_text_blocks_join_and_arguments_round_trip(self) -> None:
        manager = McpManager(_config(git=McpServerConfig(command=fake_spec(ECHO_TOOLS))))
        await manager.ensure_started()
        out = await manager.call("git", "echo", {"text": "hello"})
        lines = out.splitlines()
        assert lines[0] == "echo done"
        assert json.loads(lines[1]) == {"text": "hello"}
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_is_error_raises_mcp_error(self) -> None:
        manager = McpManager(
            _config(bad=McpServerConfig(command=fake_spec([], fail_tools=["boom"])))
        )
        await manager.ensure_started()
        with pytest.raises(McpError, match="reported an error"):
            await manager.call("bad", "boom", {})
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_call_to_unknown_server_raises(self) -> None:
        manager = McpManager(_config())
        with pytest.raises(McpError, match="not ready"):
            await manager.call("ghost", "tool", {})

    @pytest.mark.asyncio
    async def test_non_text_block_becomes_placeholder(self) -> None:
        result = {
            "content": [
                {"type": "text", "text": "here"},
                {"type": "image", "data": "AAAA", "mimeType": "image/png"},
            ]
        }
        text = _content_to_text(result, "srv")
        assert text.splitlines()[1] == "[non-text content block: image]"


# ── Registration ───────────────────────────────────────────────────────


class TestRegisterMcpTools:
    @pytest.mark.asyncio
    async def test_names_provenance_and_conservative_metadata(self) -> None:
        registry = ToolRegistry()
        dispatcher = ToolDispatcher(registry)
        manager = McpManager(_config(srv=McpServerConfig(command=fake_spec(ECHO_TOOLS))))
        await manager.ensure_started()

        count = register_mcp_tools(registry, dispatcher, manager)
        assert count == 2

        echo = registry.require("mcp__srv__echo")
        assert echo.source == "mcp:srv"
        assert echo.side_effect_class == "ask"
        assert echo.parallel_safe is False
        assert echo.path_fields == () and echo.host_fields == ()
        # Empty advertised description gets a synthesized one.
        weird = registry.require("mcp__srv__weird")
        assert weird.description != ""
        # Non-object schema sanitized into something the registry accepts.
        assert weird.parameters == {"type": "object", "properties": {}}
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_handler_routes_through_manager_and_drops_reserved_keys(
        self,
    ) -> None:
        registry = ToolRegistry()
        dispatcher = ToolDispatcher(registry)
        manager = McpManager(_config(srv=McpServerConfig(command=fake_spec(ECHO_TOOLS))))
        await manager.ensure_started()
        register_mcp_tools(registry, dispatcher, manager)

        handler = dispatcher._handlers["mcp__srv__echo"]
        out = await handler(session=None, tool_call_id="t1", text="ping")
        assert json.loads(out.splitlines()[1]) == {"text": "ping"}

        unknown = dispatcher._handlers["mcp__srv__weird"]
        out = await unknown(session=None, tool_call_id="t2", anything=1)
        assert out.startswith("weird done")
        await manager.aclose()


# ── Config schema ──────────────────────────────────────────────────────


class TestConfigSchema:
    def test_command_accepts_string_or_argv(self) -> None:
        as_string = McpServerConfig(command="uvx mcp-server-git")
        as_argv = McpServerConfig(command=["uvx", "mcp-server-git"])
        assert isinstance(as_string.command, str)
        assert as_argv.command == ["uvx", "mcp-server-git"]

    def test_both_transports_refused(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError, match="exactly one"):
            McpServerConfig(command="foo", url="http://127.0.0.1:9000/mcp")

    def test_no_transport_refused(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError, match="is required"):
            McpServerConfig()

    def test_remote_url_refused_at_load(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError, match="loopback"):
            McpServerConfig(url="https://api.example.com/mcp")

    def test_bad_server_name_refused(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError, match="letters, digits"):
            McpConfig(servers={"bad name!": McpServerConfig(command="foo")})


@pytest.mark.asyncio
async def test_manager_survives_concurrent_ensure_started() -> None:
    """Two sessions attaching at once start each server exactly once."""
    manager = McpManager(_config(git=McpServerConfig(command=fake_spec(ECHO_TOOLS))))
    await asyncio.gather(manager.ensure_started(), manager.ensure_started())
    assert len(manager.tools()) == 2
    await manager.aclose()
