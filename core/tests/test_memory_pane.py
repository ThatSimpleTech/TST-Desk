"""Workspace memory listing for the Memory pane (TD-2601)."""

from __future__ import annotations

import json
from pathlib import Path

from tstd.context.memory_loader import list_workspace_memory
from tstd.daemon import Daemon
from tstd.memory_store import memory_dir


class TestList:
    def test_missing_dir_is_empty(self, tmp_path: Path) -> None:
        assert list_workspace_memory(tmp_path) == ()

    def test_index_first_then_sorted_topics(self, tmp_path: Path) -> None:
        root = memory_dir(tmp_path)
        root.mkdir(parents=True)
        (root / "zeta.md").write_text("z\n", encoding="utf-8")
        (root / "MEMORY.md").write_text("index\n", encoding="utf-8")
        (root / "alpha.md").write_text("a\n", encoding="utf-8")
        names = [f.name for f in list_workspace_memory(tmp_path)]
        assert names == ["MEMORY.md", "alpha.md", "zeta.md"]
        assert list_workspace_memory(tmp_path)[0].content == "index\n"

    def test_skips_steering_basename(self, tmp_path: Path) -> None:
        root = memory_dir(tmp_path)
        root.mkdir(parents=True)
        (root / "AGENTS.md").write_text("no\n", encoding="utf-8")
        (root / "MEMORY.md").write_text("yes\n", encoding="utf-8")
        assert [f.name for f in list_workspace_memory(tmp_path)] == ["MEMORY.md"]


class TestDaemon:
    async def test_list_memory_is_not_a_tool(self, tmp_path: Path) -> None:
        root = memory_dir(tmp_path)
        root.mkdir(parents=True)
        (root / "MEMORY.md").write_text("durable: ruff\n", encoding="utf-8")
        daemon = Daemon(data_dir=tmp_path / "data")
        listed = await daemon._handle_message(
            json.dumps({"type": "list_memory", "workspace_path": str(tmp_path)}),
            None,
        )
        assert listed is not None
        payload = json.loads(listed)
        assert payload["type"] == "memory_files"
        assert [f["name"] for f in payload["files"]] == ["MEMORY.md"]
        assert payload["files"][0]["content"] == "durable: ruff\n"
        await daemon._shutdown()
