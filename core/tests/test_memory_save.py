"""Manual Memory-pane save (TD-2602).

The write is a human-path client message. It goes through the memory
store and ``MemoryCommitter``. Steering files never land. The agent
cannot invoke this path as a tool.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_checkpoint import _git, make_repo
from tstd.daemon import Daemon
from tstd.memory_commit import MEMORY_COMMIT_SUBJECT
from tstd.memory_store import MemorySaveError, memory_dir, save_workspace_memory
from tstd.tools.registry import create_registry


def _save_payload(workspace: Path, path: str, content: str) -> str:
    return json.dumps(
        {
            "type": "save_memory",
            "workspace_path": str(workspace),
            "path": path,
            "content": content,
        }
    )


class TestStore:
    def test_writes_existing_memory_file(self, tmp_path: Path) -> None:
        root = memory_dir(tmp_path)
        root.mkdir(parents=True)
        target = root / "MEMORY.md"
        target.write_text("old\n", encoding="utf-8")
        written = save_workspace_memory(tmp_path, "MEMORY.md", "durable: ruff\n", 200)
        assert written == target
        assert target.read_text(encoding="utf-8") == "durable: ruff\n"

    def test_refuses_steering_basename(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        agents.write_text("existing\n", encoding="utf-8")
        planted = memory_dir(tmp_path)
        planted.mkdir(parents=True)
        (planted / "AGENTS.md").write_text("no\n", encoding="utf-8")
        with pytest.raises(MemorySaveError):
            save_workspace_memory(tmp_path, "AGENTS.md", "override\n", 200)
        assert agents.read_text(encoding="utf-8") == "existing\n"
        assert (planted / "AGENTS.md").read_text(encoding="utf-8") == "no\n"

    def test_refuses_rules_path(self, tmp_path: Path) -> None:
        rule = tmp_path / ".tst" / "rules" / "api.md"
        with pytest.raises(MemorySaveError):
            save_workspace_memory(tmp_path, ".tst/rules/api.md", "never\n", 200)
        assert not rule.exists()

    def test_refuses_missing_file(self, tmp_path: Path) -> None:
        memory_dir(tmp_path).mkdir(parents=True)
        with pytest.raises(MemorySaveError):
            save_workspace_memory(tmp_path, "topic.md", "new\n", 200)
        assert not (memory_dir(tmp_path) / "topic.md").exists()

    def test_pane_save_may_replace_at_cap(self, tmp_path: Path) -> None:
        """Human correction is not fs_write. Distill and the pane may replace at cap."""
        root = memory_dir(tmp_path)
        root.mkdir(parents=True)
        target = root / "MEMORY.md"
        target.write_text("a\nb\nc\n", encoding="utf-8")
        written = save_workspace_memory(tmp_path, "MEMORY.md", "short\n", 3)
        assert written == target
        assert target.read_text(encoding="utf-8") == "short\n"


class TestDaemon:
    async def test_save_writes_and_lists(self, tmp_path: Path) -> None:
        root = memory_dir(tmp_path)
        root.mkdir(parents=True)
        (root / "MEMORY.md").write_text("old\n", encoding="utf-8")
        daemon = Daemon(data_dir=tmp_path / "data")
        listed = await daemon._handle_message(
            _save_payload(tmp_path, "MEMORY.md", "durable: ruff\n"),
            None,
        )
        assert listed is not None
        payload = json.loads(listed)
        assert payload["type"] == "memory_files"
        assert payload["files"][0]["content"] == "durable: ruff\n"
        assert (root / "MEMORY.md").read_text(encoding="utf-8") == "durable: ruff\n"
        await daemon._shutdown()

    async def test_save_commits_on_head(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        root = memory_dir(repo)
        root.mkdir(parents=True)
        (root / "MEMORY.md").write_text("old\n", encoding="utf-8")
        _git(repo, "add", ".tst/memory/MEMORY.md")
        _git(repo, "commit", "-m", "memory scaffold")
        before = _git(repo, "rev-parse", "HEAD")
        daemon = Daemon(data_dir=tmp_path / "data")
        await daemon._handle_message(
            _save_payload(repo, "MEMORY.md", "durable: ruff\n"),
            None,
        )
        assert _git(repo, "log", "-1", "--format=%s") == MEMORY_COMMIT_SUBJECT
        assert _git(repo, "rev-parse", "HEAD") != before
        await daemon._shutdown()

    async def test_steering_path_does_not_write(self, tmp_path: Path) -> None:
        agents = tmp_path / "AGENTS.md"
        agents.write_text("existing\n", encoding="utf-8")
        daemon = Daemon(data_dir=tmp_path / "data")
        listed = await daemon._handle_message(
            _save_payload(tmp_path, "AGENTS.md", "override\n"),
            None,
        )
        assert listed is not None
        payload = json.loads(listed)
        assert payload["type"] == "error"
        assert payload["code"] == "not_a_memory_file"
        assert agents.read_text(encoding="utf-8") == "existing\n"
        await daemon._shutdown()

    def test_not_in_tool_registry(self) -> None:
        names = {tool.name for tool in create_registry().list_tools()}
        assert "save_memory" not in names

    async def test_dispatcher_rejects_as_unknown_tool(self, tmp_path: Path) -> None:
        from tests.test_security_suite import make_dispatcher

        dispatcher = make_dispatcher(tmp_path)
        result = await dispatcher.dispatch(
            "t1",
            "save_memory",
            {"workspace_path": str(tmp_path), "path": "AGENTS.md", "content": "x\n"},
        )
        assert result.status == "error"
        assert result.error_code == "unknown_tool"
