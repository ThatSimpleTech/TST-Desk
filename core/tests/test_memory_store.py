"""Tests for workspace memory scaffolding (TD-2101).

Mirrors the config scaffold tests: plant on first open, never overwrite,
never invent topic files, and the daemon's open_workspace / new_session
paths both reach the same function.
"""

from __future__ import annotations

import json
from pathlib import Path

from tstd.daemon import Daemon
from tstd.memory_store import (
    MEMORY_FILENAMES,
    memory_dir,
    path_is_memory_file,
    scaffold_workspace_memory,
)


def _names(root: Path) -> set[str]:
    return {p.name for p in root.iterdir() if p.is_file()}


class TestScaffold:
    def test_plants_three_commented_templates(self, tmp_path: Path) -> None:
        written = scaffold_workspace_memory(tmp_path)
        root = memory_dir(tmp_path)
        assert root.is_dir()
        assert {p.name for p in written} == set(MEMORY_FILENAMES)
        assert _names(root) == set(MEMORY_FILENAMES)
        for path in written:
            text = path.read_text(encoding="utf-8")
            assert text.lstrip().startswith("<!--")
            assert text.rstrip().endswith("-->")

    def test_does_not_invent_topic_files(self, tmp_path: Path) -> None:
        scaffold_workspace_memory(tmp_path)
        extras = _names(memory_dir(tmp_path)) - set(MEMORY_FILENAMES)
        assert extras == set()

    def test_never_overwrites(self, tmp_path: Path) -> None:
        root = memory_dir(tmp_path)
        root.mkdir(parents=True)
        existing = root / "MEMORY.md"
        existing.write_text("keep this\n", encoding="utf-8")
        written = scaffold_workspace_memory(tmp_path)
        assert existing.read_text(encoding="utf-8") == "keep this\n"
        assert all(p.name != "MEMORY.md" for p in written)
        # The other two still plant when missing.
        assert {p.name for p in written} == {"decisions.md", "gotchas.md"}

    def test_second_call_is_noop(self, tmp_path: Path) -> None:
        first = scaffold_workspace_memory(tmp_path)
        assert len(first) == 3
        assert scaffold_workspace_memory(tmp_path) == []
        # Bytes unchanged, and still no extras.
        for original in first:
            assert original.read_text(encoding="utf-8").lstrip().startswith("<!--")
        assert _names(memory_dir(tmp_path)) == set(MEMORY_FILENAMES)


class TestPathPredicate:
    def test_memory_files_are_memory(self, tmp_path: Path) -> None:
        for name in MEMORY_FILENAMES:
            assert path_is_memory_file(memory_dir(tmp_path) / name, tmp_path)

    def test_skill_manifest_is_not_memory(self, tmp_path: Path) -> None:
        # TD-4502 convergence: SKILL.md joined the steering basenames in
        # every copy of that set. A distill write to .tst/memory/SKILL.md
        # must not pass as memory — it would ride the memory-write carve-out
        # past the classifier and become self-persisting prompt material.
        planted = memory_dir(tmp_path) / "SKILL.md"
        planted.parent.mkdir(parents=True)
        planted.write_text("---\ndescription: sneaky\n---\nno\n", encoding="utf-8")
        assert not path_is_memory_file(planted, tmp_path)


class TestDaemonPlantsMemory:
    async def test_open_workspace_plants_templates(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = json.dumps({"type": "open_workspace", "path": str(tmp_path)})
        response = await daemon._handle_message(raw, None)
        assert response is not None
        sid = json.loads(response)["session_id"]
        root = memory_dir(tmp_path)
        assert _names(root) == set(MEMORY_FILENAMES)

        user = root / "MEMORY.md"
        user.write_text("user notes\n", encoding="utf-8")
        raw2 = json.dumps({"type": "open_workspace", "path": str(tmp_path)})
        await daemon._handle_message(raw2, None)
        assert user.read_text(encoding="utf-8") == "user notes\n"

        runner = daemon.session_registry.get_runner(sid)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()

    async def test_new_session_plants_missing_templates(self, tmp_path: Path) -> None:
        """An old workspace that never had memory still gets it on new_session."""
        daemon = Daemon(data_dir=tmp_path / "data")
        opened = await daemon._handle_message(
            json.dumps({"type": "open_workspace", "path": str(tmp_path)}),
            None,
        )
        assert opened is not None
        sid = json.loads(opened)["session_id"]

        # Wipe the directory as if this workspace predated TD-2101.
        root = memory_dir(tmp_path)
        for path in root.iterdir():
            path.unlink()
        root.rmdir()
        assert not root.exists()

        reply = await daemon._handle_message(
            json.dumps({"type": "new_session", "session_id": sid}),
            None,
        )
        assert reply is not None
        assert json.loads(reply)["type"] == "session_state"
        assert _names(memory_dir(tmp_path)) == set(MEMORY_FILENAMES)

        runner = daemon.session_registry.get_runner(sid)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()
