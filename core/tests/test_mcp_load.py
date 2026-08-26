"""Load MCP servers from config (TD-4401).

A scripted child / loopback HTTP server stands in for a third-party
package. Observable: tools land in the registry with provenance, a
dead server is a doctor row, builtins survive, names are prefixed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from websockets.asyncio.client import connect

from tstd.config import (
    McpConfig,
    McpServerConfig,
    cached_config,
    default_config_yaml,
)
from tstd.daemon import Daemon
from tstd.keychain import KeychainError
from tstd.logging import user_data_dir
from tstd.mcp.errors import McpError
from tstd.mcp.http import assert_loopback_http_url
from tstd.mcp.loader import McpSupervisor
from tstd.protocol import PROTOCOL_VERSION
from tstd.tools import ToolDispatcher, create_registry
from tstd.tools.registry import Tool

# Tiny newline-delimited MCP speaker. Tool name is argv[1] (default echo).
_FAKE_STDIO = r"""
import json
import sys

TOOL = sys.argv[1] if len(sys.argv) > 1 else "echo"

def _read() -> dict[str, object] | None:
    line = sys.stdin.readline()
    if not line:
        return None
    parsed = json.loads(line)
    return parsed if isinstance(parsed, dict) else None

def _write(obj: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

while True:
    msg = _read()
    if msg is None:
        break
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "initialize":
        _write({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0"},
            },
        })
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        _write({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [{
                    "name": TOOL,
                    "description": "scripted echo",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }]
            },
        })
    elif method == "tools/call":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        _write({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": str(args.get("text", ""))}]},
        })
"""


def _stdio_command(script: Path, tool: str = "echo") -> list[str]:
    return [sys.executable, str(script), tool]


def _write_stdio_script(tmp_path: Path) -> Path:
    path = tmp_path / "fake_mcp.py"
    path.write_text(_FAKE_STDIO, encoding="utf-8")
    return path


def _mcp(servers: dict[str, McpServerConfig]) -> McpConfig:
    return McpConfig(servers=servers)


async def _load(
    config: McpConfig, *, handshake_timeout: float = 4.0
) -> tuple[McpSupervisor, Any, Any]:
    supervisor = McpSupervisor(
        config, handshake_timeout=handshake_timeout, call_timeout=handshake_timeout
    )
    registry = create_registry()
    dispatcher = ToolDispatcher(registry)
    try:
        await supervisor.ensure_loaded()
        supervisor.attach(registry, dispatcher)
        return supervisor, registry, dispatcher
    except BaseException:
        await supervisor.aclose()
        raise


class ScriptedMcpHttp:
    """Loopback HTTP JSON-RPC speaker for one scripted tool."""

    def __init__(self, tool_name: str = "echo") -> None:
        self.tool_name = tool_name
        self._server: asyncio.Server | None = None
        self._port = 0
        self.methods: list[str] = []
        self.dialed = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}/mcp"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        assert self._server.sockets
        self._port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.dialed = True
        try:
            await reader.readline()
            headers: dict[str, str] = {}
            while True:
                raw = await reader.readline()
                if raw in (b"\r\n", b"\n", b""):
                    break
                name, _, value = raw.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0") or "0")
            body = await reader.readexactly(length) if length else b""
            message: dict[str, Any]
            try:
                parsed = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                parsed = {}
            message = parsed if isinstance(parsed, dict) else {}
            method = str(message.get("method") or "")
            self.methods.append(method)
            reply = _http_reply(message, self.tool_name)
            if reply is None:
                payload = b""
                status = "204 No Content"
            else:
                payload = json.dumps(reply).encode("utf-8")
                status = "200 OK"
            writer.write(
                (
                    f"HTTP/1.1 {status}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(payload)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("latin-1")
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


def _http_reply(message: dict[str, Any], tool_name: str) -> dict[str, Any] | None:
    method = message.get("method")
    req_id = message.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-http", "version": "0"},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": tool_name,
                        "description": "scripted echo",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ]
            },
        }
    if method == "tools/call":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": str(args.get("text", ""))}]},
        }
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "unknown"}}


class FakeKeychain:
    def __init__(self) -> None:
        self.stored: dict[str, str] = {}

    async def get(self, provider_name: str = "openrouter") -> str:
        if provider_name not in self.stored:
            raise KeychainError(f"API key not found in keychain for {provider_name!r}.")
        return self.stored[provider_name]


class FakeProviderClient:
    scripted: Any = object()

    @classmethod
    async def from_keychain(cls, base_url: str, **kwargs: object) -> FakeProviderClient:
        return cls()

    async def chat_completion(self, request: Any) -> Any:
        return self.scripted


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    cached_config.cache_clear()
    return tmp_path


def _write_user_config(mcp_yaml: str) -> Path:
    dest = user_data_dir() / "config.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(default_config_yaml().rstrip() + "\n" + mcp_yaml + "\n", encoding="utf-8")
    cached_config.cache_clear()
    return dest


async def _start_daemon(tmp: Path) -> tuple[Daemon, asyncio.Task[Any]]:
    daemon = Daemon(data_dir=tmp)
    task = asyncio.create_task(daemon.run())
    for _ in range(50):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


async def _stop_daemon(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _doctor_checks(daemon: Daemon) -> list[dict[str, Any]]:
    ws = await connect(f"ws://127.0.0.1:{daemon.ws_server.port}")
    await ws.send(
        json.dumps(
            {
                "type": "hello",
                "token": daemon.ws_server.token,
                "version": PROTOCOL_VERSION,
            }
        )
    )
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    await ws.send(json.dumps({"type": "run_diagnostics"}))
    resp = dict(json.loads(await ws.recv()))
    await ws.close()
    assert resp["type"] == "diagnostics_report"
    return list(resp["checks"])


class TestConfigShape:
    def test_env_map_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="env"):
            McpServerConfig.model_validate(
                {
                    "transport": "stdio",
                    "command": ["true"],
                    "env": {"TOKEN": "secret"},
                }
            )

    def test_server_id_must_be_a_slug(self) -> None:
        with pytest.raises(ValidationError, match="lowercase slug"):
            McpConfig.model_validate(
                {"servers": {"Nope": {"transport": "stdio", "command": ["x"]}}}
            )

    def test_command_string_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="list of argv"):
            McpServerConfig.model_validate({"transport": "stdio", "command": "python -m x"})


class TestStdioLoad:
    @pytest.mark.asyncio
    async def test_tool_appears_with_provenance(self, tmp_path: Path) -> None:
        script = _write_stdio_script(tmp_path)
        config = _mcp(
            {
                "example": McpServerConfig(
                    transport="stdio",
                    command=_stdio_command(script),
                )
            }
        )
        supervisor, registry, dispatcher = await _load(config)
        try:
            tool = registry.get("example__echo")
            assert isinstance(tool, Tool)
            assert tool.provenance == "mcp:example"
            assert tool.side_effect_class == "ask"
            builtin = registry.require("fs_read")
            assert builtin.provenance is None
            handler = dispatcher._handlers["example__echo"]
            text = await handler(session=None, tool_call_id="t1", text="hi")
            assert text == "hi"
        finally:
            await supervisor.aclose()

    @pytest.mark.asyncio
    async def test_mcp_fs_read_does_not_replace_builtin(self, tmp_path: Path) -> None:
        script = _write_stdio_script(tmp_path)
        config = _mcp(
            {
                "collide": McpServerConfig(
                    transport="stdio",
                    command=_stdio_command(script, "fs_read"),
                )
            }
        )
        supervisor, registry, _dispatcher = await _load(config)
        try:
            builtin = registry.require("fs_read")
            assert builtin.provenance is None
            assert "Read a text file" in builtin.description
            mcp_tool = registry.require("collide__fs_read")
            assert mcp_tool.provenance == "mcp:collide"
        finally:
            await supervisor.aclose()


class TestHttpLoad:
    @pytest.mark.asyncio
    async def test_loopback_http_registers_tool(self) -> None:
        server = ScriptedMcpHttp()
        await server.start()
        try:
            config = _mcp({"loop": McpServerConfig(transport="http", url=server.url)})
            supervisor, registry, dispatcher = await _load(config)
            try:
                tool = registry.require("loop__echo")
                assert tool.provenance == "mcp:loop"
                handler = dispatcher._handlers["loop__echo"]
                text = await handler(session=None, tool_call_id="t1", text="pong")
                assert text == "pong"
                assert "initialize" in server.methods
                assert "tools/list" in server.methods
                assert "tools/call" in server.methods
            finally:
                await supervisor.aclose()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_non_loopback_url_is_refused(self) -> None:
        with pytest.raises(McpError, match="loopback"):
            assert_loopback_http_url("https://example.invalid/mcp")
        config = _mcp(
            {
                "remote": McpServerConfig(
                    transport="http",
                    url="https://example.invalid/mcp",
                )
            }
        )
        supervisor = McpSupervisor(config, handshake_timeout=1.0, call_timeout=1.0)
        try:
            rows = await supervisor.ensure_loaded()
            assert len(rows) == 1
            assert rows[0].status == "fail"
            assert rows[0].fix
            assert "loopback" in rows[0].detail
            registry = create_registry()
            dispatcher = ToolDispatcher(registry)
            supervisor.attach(registry, dispatcher)
            assert registry.get("remote__echo") is None
            assert registry.get("fs_read") is not None
        finally:
            await supervisor.aclose()


class TestDeadServer:
    @pytest.mark.asyncio
    async def test_crash_on_start_keeps_builtins(self) -> None:
        config = _mcp(
            {
                "dead": McpServerConfig(
                    transport="stdio",
                    command=[sys.executable, "-c", "raise SystemExit(1)"],
                )
            }
        )
        supervisor, registry, _dispatcher = await _load(config, handshake_timeout=2.0)
        try:
            rows = supervisor.doctor_rows()
            assert rows[0].server_id == "dead"
            assert rows[0].status == "fail"
            assert rows[0].fix
            assert registry.get("fs_read") is not None
            assert registry.get("dead__echo") is None
        finally:
            await supervisor.aclose()

    @pytest.mark.asyncio
    async def test_daemon_starts_and_doctor_fails(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fk = FakeKeychain()
        fk.stored["openrouter"] = "sk-ok"

        async def _get(provider_name: str = "openrouter") -> str:
            return await fk.get(provider_name)

        monkeypatch.setattr("tstd.daemon.get_api_key", _get)
        monkeypatch.setattr("tstd.daemon.ProviderClient", FakeProviderClient)
        _write_user_config(
            "mcp:\n"
            "  servers:\n"
            "    dead:\n"
            "      transport: stdio\n"
            f"      command: [{json.dumps(sys.executable)}, '-c', 'raise SystemExit(1)']\n"
        )
        daemon, task = await _start_daemon(tmp_path / "data")
        try:
            assert daemon.ws_server.port > 0
            checks = await _doctor_checks(daemon)
            names = [c["name"] for c in checks]
            assert names[:6] == [
                "daemon",
                "api_key",
                "provider",
                "git",
                "workspace",
                "steering",
            ]
            assert "mcp:dead" in names
            row = next(c for c in checks if c["name"] == "mcp:dead")
            assert row["status"] == "fail"
            assert row["fix"]
        finally:
            await _stop_daemon(task)


class TestEmptyMcp:
    @pytest.mark.asyncio
    async def test_empty_config_adds_no_doctor_rows(self) -> None:
        supervisor = McpSupervisor(McpConfig())
        rows = await supervisor.ensure_loaded()
        assert rows == []
        assert supervisor.doctor_rows() == []
        await supervisor.aclose()

    def test_disabled_or_missing_fields(self) -> None:
        disabled = McpServerConfig(transport="stdio", command=["true"], enabled=False)
        missing_cmd = McpServerConfig(transport="stdio")
        missing_url = McpServerConfig(transport="http")
        assert disabled.enabled is False
        assert missing_cmd.command == []
        assert missing_url.url == ""


@pytest.mark.asyncio
async def test_disabled_and_missing_are_skip_or_fail(tmp_path: Path) -> None:
    script = _write_stdio_script(tmp_path)
    config = _mcp(
        {
            "off": McpServerConfig(
                transport="stdio",
                command=_stdio_command(script),
                enabled=False,
            ),
            "nocmd": McpServerConfig(transport="stdio"),
            "nourl": McpServerConfig(transport="http"),
        }
    )
    supervisor, registry, _dispatcher = await _load(config)
    try:
        by_id = {row.server_id: row for row in supervisor.doctor_rows()}
        assert by_id["off"].status == "skip"
        assert by_id["nocmd"].status == "fail"
        assert by_id["nocmd"].fix
        assert by_id["nourl"].status == "fail"
        assert by_id["nourl"].fix
        assert registry.get("fs_read") is not None
    finally:
        await supervisor.aclose()
