"""Workspace instruction listing and human-path rule create (TD-2802)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tstd.context.instructions import (
    InstructionNameError,
    create_rule_file,
    list_workspace_instructions,
    normalize_rule_name,
)
from tstd.daemon import Daemon


class TestList:
    def test_empty_workspace_lists_nothing(self, tmp_path: Path) -> None:
        assert list_workspace_instructions(tmp_path) == []

    def test_agents_md_wins_over_claude(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("claude\n", encoding="utf-8")
        files = list_workspace_instructions(tmp_path)
        assert [f.name for f in files] == ["AGENTS.md"]
        assert files[0].kind == "agents"

    def test_claude_fallback_when_agents_absent(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("claude\n", encoding="utf-8")
        files = list_workspace_instructions(tmp_path)
        assert files[0].kind == "claude"
        assert files[0].name == "CLAUDE.md"

    def test_rules_after_root_sorted_by_name(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("root\n", encoding="utf-8")
        rules = tmp_path / ".tst" / "rules"
        rules.mkdir(parents=True)
        (rules / "zeta.md").write_text("z\n", encoding="utf-8")
        (rules / "alpha.md").write_text("a\n", encoding="utf-8")
        names = [f.name for f in list_workspace_instructions(tmp_path)]
        assert names == ["AGENTS.md", "alpha.md", "zeta.md"]
        assert [f.kind for f in list_workspace_instructions(tmp_path)] == [
            "agents",
            "rule",
            "rule",
        ]

    def test_nested_agents_are_not_listed(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "AGENTS.md").write_text("nested\n", encoding="utf-8")
        assert list_workspace_instructions(tmp_path) == []


class TestCreate:
    def test_plants_commented_rule(self, tmp_path: Path) -> None:
        path = create_rule_file(tmp_path, "api")
        assert path == tmp_path / ".tst" / "rules" / "api.md"
        text = path.read_text(encoding="utf-8")
        assert text.lstrip().startswith("<!--")
        assert "steering.md" in text

    def test_never_overwrites(self, tmp_path: Path) -> None:
        dest = tmp_path / ".tst" / "rules" / "api.md"
        dest.parent.mkdir(parents=True)
        dest.write_text("keep\n", encoding="utf-8")
        assert create_rule_file(tmp_path, "api.md").read_text(encoding="utf-8") == "keep\n"

    def test_rejects_path_separators(self, tmp_path: Path) -> None:
        with pytest.raises(InstructionNameError):
            create_rule_file(tmp_path, "../escape")
        with pytest.raises(InstructionNameError):
            create_rule_file(tmp_path, "a/b")
        assert not (tmp_path / ".tst").exists()

    def test_rejects_agents_basename(self) -> None:
        with pytest.raises(InstructionNameError):
            normalize_rule_name("AGENTS.md")
        with pytest.raises(InstructionNameError):
            normalize_rule_name("claude")


class TestDaemon:
    async def test_list_and_create_are_not_tools(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("root\n", encoding="utf-8")
        daemon = Daemon(data_dir=tmp_path / "data")
        listed = await daemon._handle_message(
            json.dumps({"type": "list_instructions", "workspace_path": str(tmp_path)}),
            None,
        )
        assert listed is not None
        payload = json.loads(listed)
        assert payload["type"] == "instruction_files"
        assert [f["name"] for f in payload["files"]] == ["AGENTS.md"]
        assert payload["created"] is None

        created = await daemon._handle_message(
            json.dumps({"type": "create_rule", "workspace_path": str(tmp_path), "name": "api"}),
            None,
        )
        assert created is not None
        reply = json.loads(created)
        assert reply["type"] == "instruction_files"
        assert reply["created"].endswith("api.md")
        assert [f["name"] for f in reply["files"]] == ["AGENTS.md", "api.md"]
        rule = tmp_path / ".tst" / "rules" / "api.md"
        assert rule.is_file()
        # Human path, not fs_write: no classifier, no tool_result.
        await daemon._shutdown()

    async def test_bad_name_is_typed_error(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = json.dumps({"type": "create_rule", "workspace_path": str(tmp_path), "name": "../x"})
        reply = await daemon._handle_message(raw, None)
        assert reply is not None
        err = json.loads(reply)
        assert err["type"] == "error"
        assert err["code"] == "invalid_rule_name"
        await daemon._shutdown()

    async def test_missing_workspace_is_typed_error(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = json.dumps(
            {
                "type": "list_instructions",
                "workspace_path": str(tmp_path / "nope"),
            }
        )
        reply = await daemon._handle_message(raw, None)
        assert reply is not None
        assert json.loads(reply)["code"] == "workspace_not_found"
        await daemon._shutdown()
