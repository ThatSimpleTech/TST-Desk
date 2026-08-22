"""Writing MCP server entries back to the user config (TD-4403).

The settings screen adds, disables, and removes servers without the user
editing YAML. The write is surgical like the tier writers — the shipped
config's comments survive — with one extra shape: the shipped inline-empty
``servers: {}`` grows into a block on first edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tstd.config import ConfigError, ensure_user_config, load_config
from tstd.config_write import remove_mcp_server, save_mcp_enabled, save_mcp_server


def _user_config(tmp_path: Path) -> Path:
    """A fresh copy of the shipped config, as a first run would leave it."""
    return ensure_user_config(tmp_path / "config.yaml")


def _servers(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    servers = data["mcp"]["servers"]
    assert isinstance(servers, dict)
    return servers


class TestSaveServer:
    def test_first_entry_grows_the_inline_empty_form(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        assert "servers: {}" in path.read_text(encoding="utf-8")

        save_mcp_server("git", ["uvx", "mcp-server-git"], path)

        assert _servers(path) == {"git": {"command": ["uvx", "mcp-server-git"]}}
        assert load_config(path).mcp.servers["git"].command == ["uvx", "mcp-server-git"]
        # The block form replaced the flow form; no stray braces remain.
        text = path.read_text(encoding="utf-8")
        assert "servers: {}" not in text
        assert "  servers:" in text

    def test_second_entry_joins_the_block(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        save_mcp_server("git", ["uvx", "mcp-server-git"], path)
        save_mcp_server("docs", ["mcp-server-fetch"], path)

        assert sorted(_servers(path)) == ["docs", "git"]
        # The sibling entry is untouched.
        assert _servers(path)["git"] == {"command": ["uvx", "mcp-server-git"]}

    def test_replace_is_wholesale(self, tmp_path: Path) -> None:
        """A replaced entry is exactly command — a hand-added url or enabled
        cannot survive next to a new command (the one-transport rule)."""
        path = _user_config(tmp_path)
        save_mcp_server("git", ["uvx", "mcp-server-git"], path)
        text = path.read_text(encoding="utf-8").replace(
            "      command:", '      url: "http://127.0.0.1:9/mcp"\n      command:'
        )
        path.write_text(text, encoding="utf-8")

        save_mcp_server("git", ["fresh", "server"], path)

        assert _servers(path) == {"git": {"command": ["fresh", "server"]}}

    def test_rejects_a_bad_name(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        with pytest.raises(ConfigError, match="name"):
            save_mcp_server("bad name!", ["x"], path)

    def test_rejects_an_empty_argv(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        with pytest.raises(ConfigError, match="argv"):
            save_mcp_server("git", [], path)
        with pytest.raises(ConfigError, match="argv"):
            save_mcp_server("git", [" "], path)

    def test_missing_mcp_block_names_the_fix(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        text = path.read_text(encoding="utf-8")
        cut = text.index("mcp:")
        path.write_text(text[:cut], encoding="utf-8")
        with pytest.raises(ConfigError, match="mcp:"):
            save_mcp_server("git", ["x"], path)


class TestSaveEnabled:
    def test_toggles_a_live_enabled_line(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        save_mcp_server("git", ["uvx", "mcp-server-git"], path)
        save_mcp_enabled("git", False, path)

        assert _servers(path)["git"]["enabled"] is False
        save_mcp_enabled("git", True, path)
        assert _servers(path)["git"]["enabled"] is True

    def test_appends_when_absent(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        save_mcp_server("git", ["uvx", "mcp-server-git"], path)
        save_mcp_enabled("git", False, path)

        loaded = load_config(path).mcp.servers["git"]
        assert loaded.enabled is False
        assert loaded.command == ["uvx", "mcp-server-git"]

    def test_unknown_server_lists_known_ones(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        save_mcp_server("git", ["uvx", "mcp-server-git"], path)
        with pytest.raises(ConfigError, match="git"):
            save_mcp_enabled("nope", True, path)


class TestRemoveServer:
    def test_removes_one_entry(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        save_mcp_server("git", ["uvx", "mcp-server-git"], path)
        save_mcp_server("docs", ["mcp-server-fetch"], path)

        remove_mcp_server("git", path)

        assert list(_servers(path)) == ["docs"]

    def test_last_entry_collapses_to_inline_empty(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        save_mcp_server("git", ["uvx", "mcp-server-git"], path)

        remove_mcp_server("git", path)

        assert "servers: {}" in path.read_text(encoding="utf-8")
        assert load_config(path).mcp.servers == {}

    def test_unknown_server_raises(self, tmp_path: Path) -> None:
        path = _user_config(tmp_path)
        with pytest.raises(ConfigError, match="nope"):
            remove_mcp_server("nope", path)


class TestSurgical:
    def test_comments_outside_the_section_survive(self, tmp_path: Path) -> None:
        """The teaching comments are why these writers exist."""
        path = _user_config(tmp_path)
        before = path.read_text(encoding="utf-8")
        marker = "compaction budget"
        assert marker in before
        prefix = before.partition("mcp:")[0]
        assert marker in prefix

        save_mcp_server("git", ["uvx", "mcp-server-git"], path)

        # Everything above the section is byte-identical.
        assert path.read_text(encoding="utf-8").startswith(prefix)
