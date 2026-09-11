"""Grok home listing and loopback URL sniffing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tstd.grok_home import (
    acp_mcp_servers,
    computer_use_mcp,
    grok_configured_model,
    mcp_servers_for_acp,
    media_kind,
    open_in_terminal,
    resolve_workspace_file,
    sniff_local_url,
)


def test_sniff_local_url() -> None:
    assert sniff_local_url("up at http://127.0.0.1:5173/app") == "http://127.0.0.1:5173/app"
    assert sniff_local_url("also http://localhost:3000/") == "http://localhost:3000/"
    assert sniff_local_url("see https://example.com") is None


def test_media_kind() -> None:
    assert media_kind("out.png") == "image"
    assert media_kind("clip.mp4") == "video"
    assert media_kind("index.html") == "html"
    assert media_kind("notes.md") is None


def test_resolve_workspace_file(tmp_path: Path) -> None:
    inside = tmp_path / "shot.png"
    inside.write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "outside").mkdir()
    assert resolve_workspace_file(str(tmp_path), "shot.png") == inside.resolve()
    assert resolve_workspace_file(str(tmp_path), str(inside)) == inside.resolve()
    assert resolve_workspace_file(str(tmp_path), "../shot.png") is None
    assert resolve_workspace_file(str(tmp_path), "missing.png") is None


def test_grok_configured_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tstd.grok_home as grok_home

    monkeypatch.setattr(grok_home, "grok_home", lambda: tmp_path)
    assert grok_configured_model() is None
    (tmp_path / "config.toml").write_text('[models]\ndefault = "grok-4.6"\n', encoding="utf-8")
    assert grok_configured_model() == "grok-4.6"


def test_list_grok_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tstd.grok_home as grok_home

    monkeypatch.setattr(grok_home, "grok_home", lambda: tmp_path)
    sess = tmp_path / "sessions" / "cwd" / "abc-id"
    sess.mkdir(parents=True)
    (sess / "summary.json").write_text(
        json.dumps(
            {
                "generated_title": "Fix login",
                "info": {"session_id": "abc-id", "cwd": "/tmp/proj"},
                "updated_at": 100,
            }
        ),
        encoding="utf-8",
    )
    rows = grok_home.list_grok_sessions()
    assert rows[0]["id"] == "abc-id"
    assert rows[0]["title"] == "Fix login"


def test_list_grok_extensions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tstd.grok_home as grok_home

    monkeypatch.setattr(grok_home, "grok_home", lambda: tmp_path)
    (tmp_path / "config.toml").write_text(
        '[mcp_servers.docs]\ncommand = "docs-mcp"\n\n'
        '[mcp_servers.hidden]\ncommand = "secret-mcp"\nenabled = false\n',
        encoding="utf-8",
    )
    skill = tmp_path / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")
    (tmp_path / "plugins" / "pack").mkdir(parents=True)
    items = grok_home.list_grok_extensions()
    kinds = {(row["kind"], row["name"]) for row in items}
    assert ("mcp", "docs") in kinds
    assert ("mcp", "hidden") in kinds
    assert ("skill", "review") in kinds
    assert ("plugin", "pack") in kinds
    hidden = next(row for row in items if row["name"] == "hidden")
    assert hidden["detail"].startswith("disabled")
    (tmp_path / "auth.json").write_text(
        '{"api_key": "sk-not-a-real-key"}',  # tst-secret-ok
        encoding="utf-8",
    )
    dumped = json.dumps(grok_home.list_grok_extensions())
    assert "sk-not-a-real-key" not in dumped  # tst-secret-ok


def test_mcp_servers_for_acp_skips_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tstd.grok_home as grok_home

    monkeypatch.setattr(grok_home, "grok_home", lambda: tmp_path)
    (tmp_path / "config.toml").write_text(
        '[mcp_servers.docs]\ncommand = "docs-mcp"\nargs = ["--stdio"]\n\n'
        '[mcp_servers.hidden]\ncommand = "secret-mcp"\nenabled = false\n',
        encoding="utf-8",
    )
    rows = mcp_servers_for_acp()
    assert rows == [{"name": "docs", "command": "docs-mcp", "args": ["--stdio"]}]


def test_computer_use_mcp_from_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TST_CU_AGENT_SOCK", raising=False)
    binary = tmp_path / "tst-cu-mcp"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    spec = computer_use_mcp(str(binary))
    assert spec is not None
    assert spec["name"] == "computer-use"
    assert spec["command"] == str(binary.resolve())
    # A configured command is the daemon's driver too: it paints the ring
    # from cu_session tags, so this child is told not to.
    assert spec["env"] == [{"name": "TST_CU_MCP_OVERLAY", "value": "0"}]


def test_acp_mcp_servers_merges_computer_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tstd.grok_home as grok_home

    monkeypatch.setattr(grok_home, "grok_home", lambda: tmp_path)
    (tmp_path / "config.toml").write_text(
        '[mcp_servers.docs]\ncommand = "docs-mcp"\n', encoding="utf-8"
    )
    binary = tmp_path / "tst-cu-mcp"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    rows = acp_mcp_servers(str(binary))
    names = [row["name"] for row in rows]
    assert "docs" in names
    assert "computer-use" in names


def test_computer_use_mcp_finds_checkout_from_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tstd.grok_home as grok_home

    (tmp_path / "pkg" / "tstd").mkdir(parents=True)
    monkeypatch.setattr(grok_home, "__file__", str(tmp_path / "pkg" / "tstd" / "grok_home.py"))
    monkeypatch.setattr(grok_home.shutil, "which", lambda _name=None: None)
    binary = (
        tmp_path / "Documents" / "TST-Desk" / "mcp" / "tst-cu-mcp" / ".venv" / "bin" / "tst-cu-mcp"
    )
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    spec = computer_use_mcp("", search_from=str(tmp_path / "Documents"))
    assert spec is not None
    assert spec["command"] == str(binary.resolve())
    # No configured driver means nobody else paints; this child keeps the ring.
    assert {"name": "TST_CU_MCP_OVERLAY", "value": "0"} not in spec["env"]


def test_open_in_terminal_rejects_control_chars() -> None:
    with pytest.raises(ValueError, match="unusable"):
        open_in_terminal("abc\nid", "/tmp")
