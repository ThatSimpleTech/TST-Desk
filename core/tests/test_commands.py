"""Slash-command discovery (TD-4501): sources, precedence, containment."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tstd.commands import (
    COMMAND_SOFT_LINE_LIMIT,
    discover_commands,
)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def plant(root: Path, relpath: str, text: str) -> Path:
    """Sync helper: write a command file (ASYNC240 keeps Path out of async)."""
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestWorkspaceDiscovery:
    def test_workspace_commands_are_found(self, ws: Path, home: Path) -> None:
        plant(ws, ".tst/commands/deploy.md", "Deploy the thing\n")
        found = discover_commands(ws, home=home)
        assert [c.name for c in found] == ["deploy"]
        assert found[0].source == "workspace"
        assert found[0].body == "Deploy the thing\n"
        assert found[0].line_count == 1

    def test_frontmatter_description_is_parsed(self, ws: Path, home: Path) -> None:
        plant(
            ws,
            ".tst/commands/deploy.md",
            "---\ndescription: Ship to staging\n---\nDeploy the thing\n",
        )
        (found,) = discover_commands(ws, home=home)
        assert found.description == "Ship to staging"
        assert found.body == "Deploy the thing\n"

    def test_blank_description_is_none(self, ws: Path, home: Path) -> None:
        plant(ws, ".tst/commands/x.md", "---\ndescription: '  '\n---\nbody\n")
        (found,) = discover_commands(ws, home=home)
        assert found.description is None

    def test_results_are_sorted_by_name(self, ws: Path, home: Path) -> None:
        plant(ws, ".tst/commands/zulu.md", "z\n")
        plant(ws, ".tst/commands/alpha.md", "a\n")
        assert [c.name for c in discover_commands(ws, home=home)] == ["alpha", "zulu"]

    def test_only_top_level_md_files(self, ws: Path, home: Path) -> None:
        plant(ws, ".tst/commands/nested/deep.md", "d\n")
        plant(ws, ".tst/commands/notes.txt", "t\n")
        assert discover_commands(ws, home=home) == []


class TestPrecedence:
    def test_user_global_wins_on_collision(self, ws: Path, home: Path) -> None:
        plant(ws, ".tst/commands/deploy.md", "workspace body\n")
        plant(home, ".tstdesk/commands/deploy.md", "user body\n")
        (found,) = discover_commands(ws, home=home)
        assert found.source == "user"
        assert found.body == "user body\n"

    def test_workspace_only_names_survive_the_collision(self, ws: Path, home: Path) -> None:
        plant(ws, ".tst/commands/deploy.md", "w\n")
        plant(ws, ".tst/commands/keep.md", "k\n")
        plant(home, ".tstdesk/commands/deploy.md", "u\n")
        found = {c.name: c for c in discover_commands(ws, home=home)}
        assert found["keep"].source == "workspace"
        assert found["deploy"].source == "user"

    def test_claude_fallback_when_workspace_dir_missing(self, ws: Path, home: Path) -> None:
        plant(ws, ".claude/commands/review.md", "review body\n")
        (found,) = discover_commands(ws, home=home)
        assert found.source == "workspace_fallback"
        assert found.body == "review body\n"

    def test_claude_fallback_when_workspace_dir_empty(self, ws: Path, home: Path) -> None:
        (ws / ".tst" / "commands").mkdir(parents=True)
        plant(ws, ".claude/commands/review.md", "review body\n")
        (found,) = discover_commands(ws, home=home)
        assert found.source == "workspace_fallback"

    def test_own_dir_presence_suppresses_fallback(self, ws: Path, home: Path) -> None:
        plant(ws, ".tst/commands/own.md", "own\n")
        plant(ws, ".claude/commands/shadowed.md", "shadow\n")
        assert [c.name for c in discover_commands(ws, home=home)] == ["own"]

    def test_user_claude_fallback(self, ws: Path, home: Path) -> None:
        plant(home, ".claude/commands/home-review.md", "home review\n")
        (found,) = discover_commands(ws, home=home)
        assert found.source == "user_fallback"

    def test_workspace_fallback_loses_to_user_own(self, ws: Path, home: Path) -> None:
        plant(ws, ".claude/commands/deploy.md", "ws fallback\n")
        plant(home, ".tstdesk/commands/deploy.md", "user own\n")
        (found,) = discover_commands(ws, home=home)
        assert found.source == "user"


class TestRobustness:
    def test_missing_dirs_yield_empty(self, ws: Path, home: Path) -> None:
        assert discover_commands(ws, home=home) == []

    def test_home_defaults_to_path_home(self, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(ws.parent))
        plant(ws.parent, ".tstdesk/commands/homed.md", "from home\n")
        assert [c.name for c in discover_commands(ws)] == ["homed"]

    def test_symlink_escaping_its_dir_is_skipped(self, ws: Path, home: Path) -> None:
        outside = plant(ws.parent, "outside.md", "escaped\n")
        commands_dir = ws / ".tst" / "commands"
        commands_dir.mkdir(parents=True)
        os.symlink(outside, commands_dir / "escape.md")
        assert discover_commands(ws, home=home) == []

    def test_non_utf8_file_is_skipped(self, ws: Path, home: Path) -> None:
        commands_dir = ws / ".tst" / "commands"
        commands_dir.mkdir(parents=True)
        (commands_dir / "binary.md").write_bytes(b"\xff\xfe\x00bad")
        plant(ws, ".tst/commands/fine.md", "fine\n")
        assert [c.name for c in discover_commands(ws, home=home)] == ["fine"]

    def test_oversized_command_still_loads(self, ws: Path, home: Path) -> None:
        body = "\n".join(f"line {i}" for i in range(COMMAND_SOFT_LINE_LIMIT + 10)) + "\n"
        plant(ws, ".tst/commands/long.md", body)
        (found,) = discover_commands(ws, home=home)
        assert found.line_count == COMMAND_SOFT_LINE_LIMIT + 10

    def test_line_counts_tolerate_missing_trailing_newline(self, ws: Path, home: Path) -> None:
        plant(ws, ".tst/commands/a.md", "one\ntwo")  # no trailing newline
        (found,) = discover_commands(ws, home=home)
        assert found.line_count == 2
