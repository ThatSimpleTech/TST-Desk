"""Settings writes for listed MCP servers (TD-4403).

Add / disable / remove persist in the user ``config.yaml`` without a
YAML dump, never write ``env``, and show up on ``setup_state``. HTTP
off-box and a string command are refused before the file changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from tstd.config import (
    ConfigError,
    McpConfig,
    McpServerConfig,
    cached_config,
    ensure_user_config,
    load_config,
)
from tstd.config_write import delete_mcp_server_entry, save_mcp_server
from tstd.daemon import Daemon
from tstd.keychain import KeychainError
from tstd.mcp.loader import McpSupervisor
from tstd.protocol import SetMcpServer, parse_client_message
from tstd.tools import ToolDispatcher, create_registry

_TEACHING = "tokens stay in the keychain"


def _stdio(command: list[str], *, enabled: bool = True) -> McpServerConfig:
    return McpServerConfig(transport="stdio", command=command, enabled=enabled)


def _http(url: str, *, enabled: bool = True) -> McpServerConfig:
    return McpServerConfig(transport="http", url=url, enabled=enabled)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    cached_config.cache_clear()
    return tmp_path


def _user_config(tmp_path: Path) -> Path:
    return ensure_user_config(tmp_path / "config.yaml")


def _servers(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    servers = data.get("mcp", {}).get("servers") or {}
    assert isinstance(servers, dict)
    return servers


async def _send(daemon: Daemon, payload: dict[str, Any]) -> dict[str, Any]:
    raw = await daemon._handle_message(json.dumps(payload), None)
    assert raw is not None, f"no reply to {payload['type']}"
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


class TestPersist:
    def test_set_disable_delete(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        save_mcp_server("example", _stdio(["python", "-m", "some_mcp"]), path)
        assert _servers(path)["example"] == {
            "transport": "stdio",
            "command": ["python", "-m", "some_mcp"],
            "enabled": True,
        }

        save_mcp_server("example", _stdio(["python", "-m", "some_mcp"], enabled=False), path)
        assert _servers(path)["example"]["enabled"] is False
        assert "example" in _servers(path)

        delete_mcp_server_entry("example", path)
        assert _servers(path) == {}
        assert load_config(path).mcp.servers == {}

    def test_no_env_key_is_written(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        save_mcp_server("example", _stdio(["true"]), path)
        row = _servers(path)["example"]
        assert "env" not in row
        assert "environment" not in row
        servers_block = path.read_text(encoding="utf-8").split("mcp:", 1)[1]
        data_block = servers_block.split("notify:", 1)[0]
        assert "\n      env:" not in data_block
        assert "\n      environment:" not in data_block

    def test_http_non_loopback_refused_file_unchanged(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        before = path.read_text(encoding="utf-8")
        with pytest.raises(ConfigError, match="loopback"):
            save_mcp_server("remote", _http("https://example.invalid/mcp"), path)
        assert path.read_text(encoding="utf-8") == before
        assert "example.invalid" not in before

    def test_command_as_a_string_is_refused(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        before = path.read_text(encoding="utf-8")
        with pytest.raises(ValidationError, match="list of argv"):
            McpServerConfig.model_validate({"transport": "stdio", "command": "python -m x"})
        assert path.read_text(encoding="utf-8") == before

    def test_teaching_comments_survive(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        before = path.read_text(encoding="utf-8")
        assert _TEACHING in before
        commentary = [ln for ln in before.split("\n") if ln.strip().startswith("#")]
        assert len(commentary) > 20

        save_mcp_server("example", _stdio(["true"]), path)

        after = path.read_text(encoding="utf-8")
        assert _TEACHING in after
        for line in commentary:
            assert line in after.split("\n"), f"lost a comment: {line!r}"


class TestWire:
    def test_env_field_is_refused(self) -> None:
        with pytest.raises(Exception, match="env"):
            parse_client_message(
                json.dumps(
                    {
                        "type": "set_mcp_server",
                        "id": "example",
                        "transport": "stdio",
                        "command": ["true"],
                        "env": {"TOKEN": "secret"},
                    }
                )
            )

    def test_command_string_is_refused_at_the_wire(self) -> None:
        with pytest.raises(Exception, match="list of argv"):
            parse_client_message(
                json.dumps(
                    {
                        "type": "set_mcp_server",
                        "id": "example",
                        "transport": "stdio",
                        "command": "python -m x",
                    }
                )
            )

    def test_set_mcp_server_round_trips(self) -> None:
        msg = SetMcpServer(
            id="example",
            transport="stdio",
            command=["python", "-m", "some_mcp"],
            enabled=True,
        )
        dumped = json.loads(msg.model_dump_json())
        assert "env" not in dumped
        back = parse_client_message(json.dumps(dumped))
        assert isinstance(back, SetMcpServer)
        assert back.command == ["python", "-m", "some_mcp"]


class TestDaemonAck:
    @pytest.fixture(autouse=True)
    def _no_key(self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _missing(_provider_name: str = "openrouter") -> str:
            raise KeychainError("API key not found in keychain.")

        monkeypatch.setattr("tstd.daemon.get_api_key", _missing)
        cached_config.cache_clear()

    async def test_set_disable_delete_appear_on_setup_state(
        self, isolated_home: Path, tmp_path: Path
    ) -> None:
        ensure_user_config()
        cached_config.cache_clear()
        daemon = Daemon(data_dir=tmp_path / "data")
        path = ensure_user_config()

        added = await _send(
            daemon,
            {
                "type": "set_mcp_server",
                "id": "example",
                "transport": "stdio",
                "command": ["true"],
                "enabled": True,
            },
        )
        assert added["type"] == "setup_state"
        assert added["mcp_servers"] == [
            {
                "id": "example",
                "transport": "stdio",
                "command": ["true"],
                "url": "",
                "enabled": True,
            }
        ]
        row = _servers(path)["example"]
        assert row["command"] == ["true"]
        assert "env" not in row

        disabled = await _send(
            daemon,
            {
                "type": "set_mcp_server",
                "id": "example",
                "transport": "stdio",
                "command": ["true"],
                "enabled": False,
            },
        )
        assert disabled["mcp_servers"][0]["enabled"] is False
        assert _servers(path)["example"]["enabled"] is False

        removed = await _send(daemon, {"type": "delete_mcp_server", "id": "example"})
        assert removed["type"] == "setup_state"
        assert removed["mcp_servers"] == []
        assert _servers(path) == {}

    async def test_http_non_loopback_does_not_write(
        self, isolated_home: Path, tmp_path: Path
    ) -> None:
        path = ensure_user_config()
        cached_config.cache_clear()
        daemon = Daemon(data_dir=tmp_path / "data")
        before = path.read_text(encoding="utf-8")

        reply = await _send(
            daemon,
            {
                "type": "set_mcp_server",
                "id": "remote",
                "transport": "http",
                "url": "https://example.invalid/mcp",
                "enabled": True,
            },
        )
        assert reply["type"] == "error"
        assert "loopback" in json.dumps(reply)
        assert path.read_text(encoding="utf-8") == before

    async def test_unknown_delete_is_a_typed_error(
        self, isolated_home: Path, tmp_path: Path
    ) -> None:
        ensure_user_config()
        cached_config.cache_clear()
        daemon = Daemon(data_dir=tmp_path / "data")
        reply = await _send(daemon, {"type": "delete_mcp_server", "id": "missing"})
        assert reply["type"] == "error"
        assert "missing" in json.dumps(reply)

    async def test_command_string_does_not_write(self, isolated_home: Path, tmp_path: Path) -> None:
        path = ensure_user_config()
        cached_config.cache_clear()
        daemon = Daemon(data_dir=tmp_path / "data")
        before = path.read_text(encoding="utf-8")
        reply = await _send(
            daemon,
            {
                "type": "set_mcp_server",
                "id": "example",
                "transport": "stdio",
                "command": "python -m x",
            },
        )
        assert reply["type"] == "error"
        assert path.read_text(encoding="utf-8") == before


class TestReload:
    async def test_reload_replaces_the_listing_and_attach_still_works(self) -> None:
        first = McpConfig(
            servers={"old": _stdio(["true"], enabled=False)},
        )
        supervisor = McpSupervisor(first)
        rows = await supervisor.ensure_loaded()
        assert [r.server_id for r in rows] == ["old"]

        second = McpConfig(
            servers={"new": _stdio(["true"], enabled=False)},
        )
        rows = await supervisor.reload(second)
        assert [r.server_id for r in rows] == ["new"]
        assert supervisor.doctor_rows()[0].status == "skip"

        registry = create_registry()
        dispatcher = ToolDispatcher(registry)
        supervisor.attach(registry, dispatcher)
        assert registry.get("fs_read") is not None
        await supervisor.aclose()
        rows = await supervisor.ensure_loaded()
        assert [r.server_id for r in rows] == ["new"]
        await supervisor.aclose()
