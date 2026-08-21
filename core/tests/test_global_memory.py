"""Global memory opt-in (TD-2603).

Off by default. On appends ``~/.tstdesk/memory/`` after workspace
memory. Off never stats that directory. Those files never enter the
workspace git commit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_checkpoint import _git, make_repo
from tstd.context.embeddings import load_memory_for_turn
from tstd.context.memory_loader import load_memory_for_task
from tstd.memory_commit import MEMORY_COMMIT_SUBJECT, MemoryCommitter
from tstd.memory_pref import load_global_memory, save_global_memory
from tstd.memory_store import memory_dir


class TestPref:
    def test_absent_is_off(self, tmp_path: Path) -> None:
        assert load_global_memory(tmp_path) is False

    def test_roundtrip(self, tmp_path: Path) -> None:
        save_global_memory(tmp_path, True)
        assert load_global_memory(tmp_path) is True
        save_global_memory(tmp_path, False)
        assert load_global_memory(tmp_path) is False


class TestLoad:
    async def test_off_does_not_read_global_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_home: Path) -> object:
            raise AssertionError("off must not read ~/.tstdesk/memory")

        monkeypatch.setattr(
            "tstd.context.memory_loader.discover_global_memory",
            boom,
        )
        ws = tmp_path / "ws"
        memory_dir(ws).mkdir(parents=True)
        (memory_dir(ws) / "MEMORY.md").write_text("local\n", encoding="utf-8")
        assert load_memory_for_task(ws, "anything").names == ("MEMORY.md",)
        out = await load_memory_for_turn(ws, "anything", load_global=False)
        assert out.names == ("MEMORY.md",)

    async def test_on_appends_after_workspace(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        home = tmp_path / "home"
        memory_dir(ws).mkdir(parents=True)
        (memory_dir(ws) / "MEMORY.md").write_text("workspace index\n", encoding="utf-8")
        gdir = home / ".tstdesk" / "memory"
        gdir.mkdir(parents=True)
        (gdir / "MEMORY.md").write_text("global index\n", encoding="utf-8")
        (gdir / "auth.md").write_text("# Auth\nnever commit this\n", encoding="utf-8")
        loaded = await load_memory_for_turn(
            ws,
            "auth tokens",
            load_global=True,
            home=home,
        )
        names = loaded.names
        assert names[0] == "MEMORY.md"
        assert "auth.md" in names
        rels = [f.relative.as_posix() for f in loaded.files]
        assert any(r.startswith("~/.tstdesk/memory/") for r in rels)


class TestNeverCommitted:
    async def test_committer_ignores_global_path(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        global_file = tmp_path / "home" / ".tstdesk" / "memory" / "auth.md"
        global_file.parent.mkdir(parents=True)
        global_file.write_text("secret\n", encoding="utf-8")
        outcome = await MemoryCommitter(repo).commit([global_file])
        assert outcome.status == "skipped"
        assert _git(repo, "log", "-1", "--format=%s") != MEMORY_COMMIT_SUBJECT
