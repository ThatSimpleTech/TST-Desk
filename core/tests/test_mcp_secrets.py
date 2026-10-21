"""Keychain-backed values per MCP server (TD-5110).

A stdio child and a loopback HTTP server stand in for third-party
servers. Observable: a ``keychain:<entry>`` value reaches that one
server's child (env) or requests (headers) and no other; Settings stores
the value in the keychain and writes only the reference to config.yaml;
the value never reaches config, the log file or the audit DB.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from tstd import keychain, keychain_mcp
from tstd.audit import AuditStore
from tstd.config import McpConfig, McpServerConfig, cached_config, ensure_user_config
from tstd.daemon import Daemon
from tstd.e2e_ui import MemoryKeychain
from tstd.logging import LOG_FILE_NAME, redact_secrets, register_secret_value, setup_logging
from tstd.mcp.loader import McpSupervisor
from tstd.protocol import HandshakeError, SetMcpSecret, parse_client_message
from tstd.tools import ToolDispatcher, create_registry

# Shapes no redaction pattern knows, so only the exact-text scrub can hide them.
SECRET = "td5110-alpha-Zq8vW2nLp4Rk"
HEADER_TOKEN = "td5110-bravo-Hj3mX9cVb7Ts"

_REAL_SEAMS = {
    name: getattr(keychain_mcp, name)
    for name in ("store_mcp_secret", "get_mcp_secret", "delete_mcp_secret", "mcp_secret_is_stored")
}

# Speaks MCP on stdio. One tool, ``getenv``, answers with an env var. At
# start it writes TD5110_TOKEN to stderr, as a careless server might.
_FAKE_STDIO = r"""
import json
import os
import sys

sys.stderr.write("starting with token=" + os.environ.get("TD5110_TOKEN", "<unset>") + "\n")
sys.stderr.flush()

def _write(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method, req_id = msg.get("method"), msg.get("id")
    if method == "initialize":
        _write({"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0"}}})
    elif method == "tools/list":
        _write({"jsonrpc": "2.0", "id": req_id, "result": {"tools": [{
            "name": "getenv", "description": "read one env var",
            "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                            "required": ["name"]}}]}})
    elif method == "tools/call":
        name = msg["params"]["arguments"]["name"]
        _write({"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": os.environ.get(name, "<unset>")}]}})
    elif req_id is not None:
        _write({"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": "Method not found"}})
"""


@pytest.fixture
def memory_keychain(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryKeychain]:
    """A process-local keychain behind the real TD-5110 functions."""
    backend = MemoryKeychain()
    monkeypatch.setattr(keychain, "_backend", backend)
    for name, func in _REAL_SEAMS.items():
        monkeypatch.setattr(keychain_mcp, name, func)
    yield backend


@pytest.fixture
def fake_server(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_mcp.py"
    script.write_text(_FAKE_STDIO, encoding="utf-8")
    return [sys.executable, str(script)]


async def _load(config: McpConfig) -> tuple[McpSupervisor, Any, ToolDispatcher]:
    supervisor = McpSupervisor(config, handshake_timeout=5.0, call_timeout=5.0)
    registry = create_registry()
    dispatcher = ToolDispatcher(registry)
    await supervisor.ensure_loaded()
    supervisor.attach(registry, dispatcher)
    return supervisor, registry, dispatcher


async def _getenv(dispatcher: ToolDispatcher, server: str, name: str) -> str:
    handler = dispatcher._handlers[f"{server}__getenv"]
    text: str = await handler(session=None, tool_call_id="t1", name=name)
    return text


class RecordingMcpHttp:
    """Loopback HTTP JSON-RPC speaker that keeps every request's headers."""

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self.port = 0
        self.seen: list[dict[str, str]] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            headers: dict[str, str] = {}
            while (raw := await reader.readline()) not in (b"\r\n", b"\n", b""):
                name, _, value = raw.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            self.seen.append(headers)
            length = int(headers.get("content-length", "0") or "0")
            message = json.loads((await reader.readexactly(length)).decode() or "{}")
            payload = json.dumps(_reply(message)).encode() if "id" in message else b""
            status = "200 OK" if payload else "204 No Content"
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


def _reply(message: dict[str, Any]) -> dict[str, Any]:
    req_id = message.get("id")
    if message.get("method") == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-http", "version": "0"},
        }
    elif message.get("method") == "tools/list":
        result = {
            "tools": [
                {
                    "name": "ping",
                    "description": "pong",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        }
    else:
        result = {"content": [{"type": "text", "text": "pong"}]}
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


class TestConfigShape:
    def test_header_rules(self) -> None:
        good = McpServerConfig.model_validate(
            {
                "transport": "http",
                "url": "http://127.0.0.1:9/mcp",
                "headers": {"Authorization": "keychain:authorization", "X-Client": "tst"},
            }
        )
        assert good.headers["Authorization"] == "keychain:authorization"
        bad_maps: list[dict[str, Any]] = [
            {"Accept": "text/html"},  # the client's own
            {"Host": "elsewhere"},
            {"Mcp-Session-Id": "forged"},  # MCP's own (TD-5136)
            {"MCP-Protocol-Version": "2024-11-05"},
            {"X-A": "1", "x-a": "2"},  # same field twice
            {"Bad Name": "1"},
            {"X-A": "one\r\nInjected: yes"},
            {"X-A": "keychain:has space"},
        ]
        for bad in bad_maps:
            with pytest.raises(ValidationError, match="header"):
                McpServerConfig.model_validate(
                    {"transport": "http", "url": "http://127.0.0.1:9/mcp", "headers": bad}
                )


class TestSpawn:
    async def test_a_secret_reaches_only_its_own_child(
        self,
        memory_keychain: MemoryKeychain,
        fake_server: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TD5110_TOKEN", raising=False)
        await keychain_mcp.store_mcp_secret("alpha", "token", SECRET)
        config = McpConfig(
            servers={
                "alpha": McpServerConfig(
                    transport="stdio",
                    command=fake_server,
                    env={"TD5110_TOKEN": "keychain:token", "TD5110_MODE": "plain"},
                ),
                "beta": McpServerConfig(transport="stdio", command=fake_server),
            }
        )
        supervisor, _, dispatcher = await _load(config)
        try:
            assert [row.status for row in supervisor.doctor_rows()] == ["ok", "ok"]
            assert await _getenv(dispatcher, "alpha", "TD5110_TOKEN") == SECRET
            assert await _getenv(dispatcher, "alpha", "TD5110_MODE") == "plain"
            assert await _getenv(dispatcher, "beta", "TD5110_TOKEN") == "<unset>"
            assert await _getenv(dispatcher, "beta", "TD5110_MODE") == "<unset>"
            assert "TD5110_TOKEN" not in os.environ
            # The child still has the daemon's environment underneath.
            assert await _getenv(dispatcher, "alpha", "PATH") == os.environ["PATH"]
        finally:
            await supervisor.aclose()

    async def test_a_missing_item_fails_that_server_and_names_it(
        self, memory_keychain: MemoryKeychain, fake_server: list[str]
    ) -> None:
        config = McpConfig(
            servers={
                "alpha": McpServerConfig(
                    transport="stdio", command=fake_server, env={"TOKEN": "keychain:token"}
                ),
                "beta": McpServerConfig(transport="stdio", command=fake_server),
            }
        )
        supervisor, registry, _ = await _load(config)
        try:
            alpha, beta = supervisor.doctor_rows()
            assert alpha.status == "fail"
            assert "tst-mcp.alpha.token" in alpha.detail
            assert "not stored" in alpha.detail
            assert alpha.fix is not None and "Settings" in alpha.fix
            assert beta.status == "ok"
            names = {tool.name for tool in registry.list_tools()}
            assert "beta__getenv" in names
            assert "alpha__getenv" not in names
        finally:
            await supervisor.aclose()

    async def test_values_on_the_wrong_transport_fail_the_server(
        self, memory_keychain: MemoryKeychain, fake_server: list[str]
    ) -> None:
        config = McpConfig(
            servers={
                "alpha": McpServerConfig(
                    transport="stdio", command=fake_server, headers={"X-A": "1"}
                ),
                "beta": McpServerConfig(
                    transport="http", url="http://127.0.0.1:9/mcp", env={"A": "1"}
                ),
            }
        )
        supervisor, _, _ = await _load(config)
        try:
            alpha, beta = supervisor.doctor_rows()
            assert (alpha.status, alpha.detail) == ("fail", "headers only apply to http servers")
            assert (beta.status, beta.detail) == ("fail", "env only applies to stdio servers")
        finally:
            await supervisor.aclose()


class TestHttpHeaders:
    async def test_a_header_goes_only_to_its_own_server(
        self, memory_keychain: MemoryKeychain
    ) -> None:
        await keychain_mcp.store_mcp_secret("alpha", "authorization", f"Bearer {HEADER_TOKEN}")
        alpha, beta = RecordingMcpHttp(), RecordingMcpHttp()
        await alpha.start()
        await beta.start()
        config = McpConfig(
            servers={
                "alpha": McpServerConfig(
                    transport="http",
                    url=alpha.url,
                    headers={"Authorization": "keychain:authorization", "X-Client": "tst"},
                ),
                "beta": McpServerConfig(transport="http", url=beta.url),
            }
        )
        supervisor, _, dispatcher = await _load(config)
        try:
            assert [row.status for row in supervisor.doctor_rows()] == ["ok", "ok"]
            assert await dispatcher._handlers["alpha__ping"](session=None, tool_call_id="t") == (
                "pong"
            )
            assert len(alpha.seen) >= 4  # initialize, initialized, tools/list, tools/call
            for headers in alpha.seen:
                assert headers["authorization"] == f"Bearer {HEADER_TOKEN}"
                assert headers["x-client"] == "tst"
                # POSTs accept both (TD-5136); the GET notice stream only SSE.
                assert headers["accept"] in (
                    "application/json, text/event-stream",
                    "text/event-stream",
                )
            assert beta.seen
            for headers in beta.seen:
                assert "authorization" not in headers
                assert "x-client" not in headers
        finally:
            await supervisor.aclose()
            await alpha.stop()
            await beta.stop()

    async def test_a_proxy_setting_never_sees_the_header(
        self, memory_keychain: MemoryKeychain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # httpx honours HTTP_PROXY / ALL_PROXY (and the macOS or Windows
        # system proxy) for loopback too unless NO_PROXY lists it, and the
        # proxy can be off the box. The header must still go only to the URL.
        await keychain_mcp.store_mcp_secret("alpha", "authorization", f"Bearer {HEADER_TOKEN}")
        alpha, proxy = RecordingMcpHttp(), RecordingMcpHttp()
        await alpha.start()
        await proxy.start()
        proxy_url = f"http://127.0.0.1:{proxy.port}"
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(name, proxy_url)
            monkeypatch.setenv(name.lower(), proxy_url)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        config = McpConfig(
            servers={
                "alpha": McpServerConfig(
                    transport="http",
                    url=alpha.url,
                    headers={"Authorization": "keychain:authorization"},
                )
            }
        )
        supervisor, _, dispatcher = await _load(config)
        try:
            assert [row.status for row in supervisor.doctor_rows()] == ["ok"]
            assert await dispatcher._handlers["alpha__ping"](session=None, tool_call_id="t") == (
                "pong"
            )
            assert proxy.seen == []
            assert len(alpha.seen) >= 4
            for headers in alpha.seen:
                assert headers["authorization"] == f"Bearer {HEADER_TOKEN}"
        finally:
            await supervisor.aclose()
            await alpha.stop()
            await proxy.stop()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    cached_config.cache_clear()
    return tmp_path


async def _send(daemon: Daemon, payload: dict[str, Any]) -> dict[str, Any]:
    raw = await daemon._handle_message(json.dumps(payload), None)
    assert raw is not None, f"no reply to {payload['type']}"
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


def _server_row(path: Path, server_id: str) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    row: dict[str, Any] = data["mcp"]["servers"][server_id]
    return row


class TestSettings:
    async def test_settings_stores_the_value_in_the_keychain_only(
        self, isolated_home: Path, tmp_path: Path, memory_keychain: MemoryKeychain
    ) -> None:
        ensure_user_config()
        cached_config.cache_clear()
        daemon = Daemon(data_dir=tmp_path / "data")
        path = daemon.config_path
        added = await _send(
            daemon,
            {"type": "set_mcp_server", "id": "alpha", "transport": "stdio", "command": ["true"]},
        )
        assert added["type"] == "setup_state"

        stored = await _send(
            daemon,
            {
                "type": "set_mcp_secret",
                "server_id": "alpha",
                "target": "env",
                "name": "GITHUB_TOKEN",
                "value": SECRET,
            },
        )
        assert stored["type"] == "setup_state"
        assert SECRET not in json.dumps(stored)
        assert stored["mcp_servers"][0]["env"] == [
            {"name": "GITHUB_TOKEN", "source": "keychain", "entry": "github_token", "stored": True}
        ]
        assert SECRET not in path.read_text(encoding="utf-8")
        assert _server_row(path, "alpha")["env"] == {"GITHUB_TOKEN": "keychain:github_token"}
        assert await keychain_mcp.get_mcp_secret("alpha", "github_token") == SECRET

        # Settings' own toggle rewrites the block and keeps the reference.
        await _send(
            daemon,
            {
                "type": "set_mcp_server",
                "id": "alpha",
                "transport": "stdio",
                "command": ["true"],
                "enabled": False,
            },
        )
        assert _server_row(path, "alpha")["env"] == {"GITHUB_TOKEN": "keychain:github_token"}

        # Saving again replaces the value under the same entry.
        await _send(
            daemon,
            {
                "type": "set_mcp_secret",
                "server_id": "alpha",
                "target": "env",
                "name": "GITHUB_TOKEN",
                "value": SECRET[::-1],
            },
        )
        assert await keychain_mcp.get_mcp_secret("alpha", "github_token") == SECRET[::-1]

        removed = await _send(
            daemon,
            {
                "type": "delete_mcp_secret",
                "server_id": "alpha",
                "target": "env",
                "name": "GITHUB_TOKEN",
            },
        )
        assert removed["mcp_servers"][0]["env"] == []
        assert "env" not in _server_row(path, "alpha")
        assert not await keychain_mcp.mcp_secret_is_stored("alpha", "github_token")

    async def test_refusals_change_nothing(
        self, isolated_home: Path, tmp_path: Path, memory_keychain: MemoryKeychain
    ) -> None:
        ensure_user_config()
        cached_config.cache_clear()
        daemon = Daemon(data_dir=tmp_path / "data")
        await _send(
            daemon,
            {"type": "set_mcp_server", "id": "alpha", "transport": "stdio", "command": ["true"]},
        )
        before = daemon.config_path.read_bytes()
        for payload, needle in (
            ({"name": "TOKEN", "value": "short"}, "at least 8"),
            ({"name": "TOKEN", "value": "tab\there-is-long"}, "printable"),
            ({"name": "TOKEN", "value": SECRET, "target": "header"}, "takes env"),
            ({"name": "1BAD", "value": SECRET}, "env name"),
            ({"name": "TOKEN", "value": SECRET, "server_id": "ghost"}, "ghost"),
        ):
            reply = await _send(
                daemon,
                {
                    "type": "set_mcp_secret",
                    "server_id": "alpha",
                    "target": "env",
                    **payload,
                },
            )
            assert reply["type"] == "error", payload
            assert needle in reply["message"], reply
            assert SECRET not in json.dumps(reply)
        assert daemon.config_path.read_bytes() == before
        assert memory_keychain._secrets == {}

    async def test_a_keychain_failure_leaves_file_memory_and_keychain_agreeing(
        self,
        isolated_home: Path,
        tmp_path: Path,
        memory_keychain: MemoryKeychain,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TD-5160: a failed Remove must not drop the name and leave the item."""
        ensure_user_config()
        cached_config.cache_clear()
        daemon = Daemon(data_dir=tmp_path / "data")
        path = daemon.config_path
        await _send(
            daemon,
            {"type": "set_mcp_server", "id": "alpha", "transport": "stdio", "command": ["true"]},
        )

        def _secret(name: str) -> dict[str, str]:
            return {
                "type": "set_mcp_secret",
                "server_id": "alpha",
                "target": "env",
                "name": name,
                "value": SECRET,
            }

        await _send(daemon, _secret("A_TOKEN"))

        async def locked(*_: Any) -> None:
            raise keychain.KeychainLockedError("the keychain is locked")

        monkeypatch.setattr(keychain_mcp, "delete_mcp_secret", locked)
        monkeypatch.setattr(keychain_mcp, "mcp_secret_is_stored", locked)
        removal = {"type": "delete_mcp_secret", "server_id": "alpha", "target": "env"}
        reply = await _send(daemon, {**removal, "name": "A_TOKEN"})
        assert reply["code"] == "keychain_locked"
        a_ref = {"A_TOKEN": "keychain:a_token"}
        assert _server_row(path, "alpha")["env"] == a_ref
        assert dict(daemon.config.mcp.servers["alpha"].env) == a_ref
        monkeypatch.setattr(keychain_mcp, "delete_mcp_secret", _REAL_SEAMS["delete_mcp_secret"])
        monkeypatch.setattr(
            keychain_mcp, "mcp_secret_is_stored", _REAL_SEAMS["mcp_secret_is_stored"]
        )
        assert await keychain_mcp.mcp_secret_is_stored("alpha", "a_token")

        # Retried, Remove takes the name and the item together.
        assert (await _send(daemon, {**removal, "name": "A_TOKEN"}))["type"] == "setup_state"
        await _send(daemon, _secret("B_TOKEN"))
        assert _server_row(path, "alpha")["env"] == {"B_TOKEN": "keychain:b_token"}
        assert not await keychain_mcp.mcp_secret_is_stored("alpha", "a_token")

    async def test_a_failed_file_write_takes_the_new_item_back(
        self,
        isolated_home: Path,
        tmp_path: Path,
        memory_keychain: MemoryKeychain,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TD-5160: Save stores the item first; a file that will not write must not orphan it."""
        from tstd import mcp_settings

        ensure_user_config()
        cached_config.cache_clear()
        daemon = Daemon(data_dir=tmp_path / "data")
        await _send(
            daemon,
            {"type": "set_mcp_server", "id": "alpha", "transport": "stdio", "command": ["true"]},
        )
        before = daemon.config_path.read_bytes()

        def unwritable(*_: Any) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(mcp_settings, "save_mcp_server", unwritable)
        with pytest.raises(OSError):
            await mcp_settings.set_mcp_secret(
                daemon.config.mcp,
                daemon.config_path,
                SetMcpSecret(server_id="alpha", target="env", name="TOKEN", value=SECRET),
            )
        assert daemon.config_path.read_bytes() == before
        assert memory_keychain._secrets == {}

    async def test_removing_the_server_deletes_its_items(
        self, isolated_home: Path, tmp_path: Path, memory_keychain: MemoryKeychain
    ) -> None:
        ensure_user_config()
        cached_config.cache_clear()
        daemon = Daemon(data_dir=tmp_path / "data")
        url = "http://127.0.0.1:9/mcp"
        await _send(
            daemon, {"type": "set_mcp_server", "id": "alpha", "transport": "http", "url": url}
        )
        await _send(
            daemon, {"type": "set_mcp_server", "id": "beta", "transport": "http", "url": url}
        )
        for server in ("alpha", "beta"):
            reply = await _send(
                daemon,
                {
                    "type": "set_mcp_secret",
                    "server_id": server,
                    "target": "header",
                    "name": "Authorization",
                    "value": f"Bearer {HEADER_TOKEN}",
                },
            )
            assert reply["type"] == "setup_state"
        assert _server_row(daemon.config_path, "alpha")["headers"] == {
            "Authorization": "keychain:authorization"
        }
        await _send(daemon, {"type": "delete_mcp_server", "id": "alpha"})
        assert not await keychain_mcp.mcp_secret_is_stored("alpha", "authorization")
        assert await keychain_mcp.mcp_secret_is_stored("beta", "authorization")

    async def test_a_new_transport_drops_and_deletes_the_old_values(
        self, isolated_home: Path, tmp_path: Path, memory_keychain: MemoryKeychain
    ) -> None:
        ensure_user_config()
        cached_config.cache_clear()
        daemon = Daemon(data_dir=tmp_path / "data")
        await _send(
            daemon,
            {"type": "set_mcp_server", "id": "alpha", "transport": "stdio", "command": ["true"]},
        )
        await _send(
            daemon,
            {
                "type": "set_mcp_secret",
                "server_id": "alpha",
                "target": "env",
                "name": "TOKEN",
                "value": SECRET,
            },
        )
        moved = await _send(
            daemon,
            {
                "type": "set_mcp_server",
                "id": "alpha",
                "transport": "http",
                "url": "http://127.0.0.1:9/mcp",
            },
        )
        assert moved["mcp_servers"][0]["env"] == []
        assert "env" not in _server_row(daemon.config_path, "alpha")
        assert not await keychain_mcp.mcp_secret_is_stored("alpha", "token")

    def test_the_wire_never_takes_a_value_on_the_server_messages(self) -> None:
        for key in ("env", "headers"):
            with pytest.raises(HandshakeError, match="set_mcp_secret") as refused:
                parse_client_message(
                    json.dumps(
                        {
                            "type": "set_mcp_server",
                            "id": "alpha",
                            "transport": "stdio",
                            "command": ["true"],
                            key: {"TOKEN": SECRET},
                        }
                    )
                )
            # A validation error quotes its input; this one must not.
            assert SECRET not in refused.value.message
        with pytest.raises(HandshakeError, match="target") as bad_target:
            parse_client_message(
                json.dumps(
                    {
                        "type": "set_mcp_secret",
                        "server_id": "alpha",
                        "target": SECRET,
                        "name": "TOKEN",
                        "value": SECRET,
                    }
                )
            )
        assert SECRET not in bad_target.value.message
        msg = SetMcpSecret(server_id="alpha", target="env", name="TOKEN", value=SECRET)
        assert SECRET not in repr(msg)


class TestRedaction:
    """Directive 2: config, the log file and the audit DB never hold the value."""

    def test_the_exact_text_scrub(self) -> None:
        value = "Bearer td5110-charlie-Kp2sN8wQ"
        assert redact_secrets(value) == value  # no pattern knows this shape
        register_secret_value(value)
        assert redact_secrets(f"sent {value}") == "sent [REDACTED]"
        assert redact_secrets("token td5110-charlie-Kp2sN8wQ") == "token [REDACTED]"
        assert redact_secrets("Bearer is a word") == "Bearer is a word"

    async def test_a_short_token_after_a_scheme_is_refused(
        self, memory_keychain: MemoryKeychain
    ) -> None:
        """TD-5160: only parts of 8+ are scrubbed alone, so a shorter token is refused."""
        for value in ("Bearer x7Kq2Zp", "Basic   ab12cd ", "token 1234567"):
            with pytest.raises(keychain.KeychainError, match="part of at least 8"):
                await keychain_mcp.store_mcp_secret("alpha", "authorization", value)
            # A value that was stored by hand still fails when read.
            await memory_keychain.set_secret(keychain_mcp.mcp_secret_account("alpha", "a"), value)
            with pytest.raises(keychain.KeychainError, match="part of at least 8"):
                await keychain_mcp.get_mcp_secret("alpha", "a")
        assert not await keychain_mcp.mcp_secret_is_stored("alpha", "authorization")
        accepted = "Bearer td5160-Kp2sN8wQ"
        await keychain_mcp.store_mcp_secret("alpha", "authorization", accepted)
        assert redact_secrets("echo td5160-Kp2sN8wQ") == "echo [REDACTED]"

    async def test_logs_and_audit_never_hold_the_value(
        self,
        tmp_path: Path,
        memory_keychain: MemoryKeychain,
        fake_server: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        secret = "td5110-delta-Vb6nQ1xRf9Lm"
        assert redact_secrets(secret) == secret
        # Stored before this process started: only the read at spawn registers it.
        await memory_keychain.set_secret("tst-mcp.alpha.token", secret)
        root = logging.getLogger()
        old_level, old_handlers = root.level, root.handlers[:]
        log_dir = tmp_path / "logs"
        audit = AuditStore(tmp_path / "audit.db")
        config = McpConfig(
            servers={
                "alpha": McpServerConfig(
                    transport="stdio", command=fake_server, env={"TD5110_TOKEN": "keychain:token"}
                )
            }
        )
        supervisor: McpSupervisor | None = None
        try:
            setup_logging(level="DEBUG", log_dir=log_dir, log_to_stdout=False)
            supervisor, _, dispatcher = await _load(config)
            echoed = await _getenv(dispatcher, "alpha", "TD5110_TOKEN")
            assert echoed == secret  # the server got it and handed it back
            log_file = log_dir / LOG_FILE_NAME
            for _ in range(100):  # stderr is drained by its own task
                if "mcp stdio stderr" in log_file.read_text(encoding="utf-8"):
                    break
                await asyncio.sleep(0.02)
            noisy = logging.getLogger("tstd.test")
            noisy.warning("tool said %s", echoed)
            noisy.warning("result", extra={"extra_fields": {"text": echoed}})
            noisy.warning("as an object %s", type("Echo", (), {"__str__": lambda _s: secret})())
            audit.append_session("s1", str(tmp_path), 0.0)
            audit.append_tool_call(
                "s1", "t1", "alpha__getenv", {"name": echoed}, "B", "success", echoed
            )
            audit.append_decision("s1", "B", f"saw {echoed}", f"because {echoed}", None)
        finally:
            if supervisor is not None:
                await supervisor.aclose()
            audit.close()
            for handler in list(root.handlers):
                if handler not in old_handlers:
                    handler.close()
            root.handlers.clear()
            root.handlers.extend(old_handlers)
            root.setLevel(old_level)
        logged = log_file.read_text(encoding="utf-8")
        assert "starting with token=[REDACTED]" in logged
        assert "tool said [REDACTED]" in logged
        assert secret not in logged
        assert secret.encode() not in (tmp_path / "audit.db").read_bytes()
        with sqlite3.connect(tmp_path / "audit.db") as conn:
            rows = conn.execute("SELECT arguments FROM tool_calls").fetchall()
        assert rows == [(json.dumps({"name": "[REDACTED]"}),)]
