"""Tests for filesystem write tools (TD-604).

Covers the acceptance criteria: ``fs_write`` creates or overwrites with
parent directories created as needed; ``fs_edit`` performs exact string
replacement, failing loudly when the target is absent or ambiguous;
every write produces a diff in the ``tool_result`` for display; writes
are atomic (temp file plus rename) — no partial file on failure; and
every write is checkpointed per TD-705.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from tests.test_checkpoint import _git, _tree_files, make_repo
from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tests.test_read_tools import make_dispatcher
from tstd.autonomy import Checkpointer
from tstd.autonomy.classifier import canonical_path
from tstd.mock import MockProvider, Script
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import create_registry, fs_edit, fs_write

# ── AC: fs_write creates or overwrites; parent dirs created ───────────


class TestFsWrite:
    async def test_creates_file_with_parent_dirs(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "a.txt"
        out = await fs_write(None, str(target), "hello\n")
        assert target.read_text() == "hello\n"
        assert "created" in out

    async def test_overwrites_existing(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("old\n")
        out = await fs_write(None, str(target), "new\n")
        assert target.read_text() == "new\n"
        assert "overwrote" in out

    async def test_append_mode(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("one\n")
        await fs_write(None, str(target), "two\n", append=True)
        assert target.read_text() == "one\ntwo\n"

    async def test_append_to_new_file_just_creates(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        await fs_write(None, str(target), "first\n", append=True)
        assert target.read_text() == "first\n"


# ── AC: fs_edit exact replacement, failing loudly ───────────────────────


class TestFsEdit:
    async def test_replaces_single_occurrence(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("keep\nchange me\nkeep\n")
        out = await fs_edit(None, str(target), "change me", "changed")
        assert target.read_text() == "keep\nchanged\nkeep\n"
        assert "replaced 1 occurrence" in out

    async def test_fails_loudly_when_target_absent(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("content\n")
        with pytest.raises(ValueError, match="not found"):
            await fs_edit(None, str(target), "absent", "x")
        assert target.read_text() == "content\n"  # untouched

    async def test_fails_loudly_when_target_ambiguous(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("dup\ndup\n")
        with pytest.raises(ValueError, match="ambiguous: 2 occurrences"):
            await fs_edit(None, str(target), "dup", "x")
        assert target.read_text() == "dup\ndup\n"  # untouched

    async def test_missing_file_errors(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="use fs_write"):
            await fs_edit(None, str(tmp_path / "nope.txt"), "a", "b")

    async def test_empty_replacement_deletes(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("keep\ndrop\nkeep\n")
        await fs_edit(None, str(target), "drop\n", "")
        assert target.read_text() == "keep\nkeep\n"


# ── AC: every write produces a diff in the tool_result ──────────────────


class TestWriteDiffs:
    # Every test dispatches workspace-relative paths from inside the
    # workspace, so the write semantics under test are the only variable:
    # an absolute tmp_path is a drive-letter path on Windows, which the
    # guard judges by different rules per host (TD-1406).

    async def test_overwrite_diff(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        target = tmp_path / "a.txt"
        target.write_text("old line\n")
        dispatcher = make_dispatcher(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = await dispatcher.dispatch(
            "c1", "fs_write", {"path": "a.txt", "content": "new line\n"}
        )
        assert result.status == "success"
        assert result.diff is not None
        assert "-old line" in result.diff
        assert "+new line" in result.diff
        # Labelled with the canonical path (the form the guard returns).
        assert str(canonical_path(Path("a.txt"))) in result.diff

    async def test_new_file_diff(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        dispatcher = make_dispatcher(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = await dispatcher.dispatch(
            "c1", "fs_write", {"path": "new.txt", "content": "first\n"}
        )
        assert result.diff is not None
        assert "+first" in result.diff

    async def test_edit_diff(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        target = tmp_path / "a.txt"
        target.write_text("alpha\nbeta\n")
        dispatcher = make_dispatcher(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = await dispatcher.dispatch(
            "c1", "fs_edit", {"path": "a.txt", "old_string": "beta", "new_string": "gamma"}
        )
        assert result.status == "success"
        assert result.diff is not None
        assert "-beta" in result.diff
        assert "+gamma" in result.diff

    async def test_no_change_write_has_no_diff(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        target = tmp_path / "a.txt"
        target.write_text("same\n")
        dispatcher = make_dispatcher(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = await dispatcher.dispatch("c1", "fs_write", {"path": "a.txt", "content": "same\n"})
        assert result.status == "success"
        assert result.diff is None  # nothing changed, nothing to display

    async def test_reads_carry_no_diff(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        target = tmp_path / "a.txt"
        target.write_text("x\n")
        dispatcher = make_dispatcher(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = await dispatcher.dispatch("c1", "fs_read", {"path": "a.txt"})
        assert result.status == "success"
        assert result.diff is None


# ── AC: writes are atomic — no partial file on failure ──────────────────


class TestAtomicity:
    async def test_failed_write_leaves_original_intact(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        target = tmp_path / "a.txt"
        target.write_text("original\n")
        monkeypatch.setattr("tstd.tools.write.os.replace", _raise_oserror)
        dispatcher = make_dispatcher(tmp_path)
        monkeypatch.chdir(tmp_path)  # workspace-relative path — TD-1406

        result = await dispatcher.dispatch(
            "c1", "fs_write", {"path": "a.txt", "content": "replacement\n"}
        )

        assert result.status == "error"
        assert result.error_code == "handler_error"
        assert target.read_text() == "original\n"  # not partial, not replaced
        assert _stray_files(tmp_path) == []  # temp file cleaned up

    async def test_failed_edit_leaves_original_intact(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        target = tmp_path / "a.txt"
        target.write_text("original\n")
        monkeypatch.setattr("tstd.tools.write.os.replace", _raise_oserror)
        dispatcher = make_dispatcher(tmp_path)
        monkeypatch.chdir(tmp_path)  # workspace-relative path — TD-1406

        result = await dispatcher.dispatch(
            "c1", "fs_edit", {"path": "a.txt", "old_string": "original", "new_string": "new"}
        )

        assert result.status == "error"
        assert target.read_text() == "original\n"
        assert _stray_files(tmp_path) == []


def _raise_oserror(*args: object) -> None:
    raise OSError("disk full")


def _stray_files(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if p.name != "a.txt"]


# ── AC: every write is checkpointed per TD-705 ──────────────────────────


class TestWriteCheckpoints:
    # Workspace-relative paths dispatched from inside the repo — TD-1406
    # (an absolute tmp_path is a drive-letter path on Windows, judged by
    # different guard rules per host).

    async def test_fs_write_checkpoints(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        repo = make_repo(tmp_path)
        session = Session(str(repo))
        dispatcher = make_dispatcher(repo)
        dispatcher.checkpointer = Checkpointer(repo, session.id)
        monkeypatch.chdir(repo)

        result = await dispatcher.dispatch(
            "c1",
            "fs_write",
            {"path": "b.txt", "content": "written\n"},
            session=session,
        )

        assert result.status == "success"
        assert result.checkpoint_commit is not None
        tip = _git(repo, "rev-parse", f"refs/heads/tst/session/{session.id}")
        assert tip == result.checkpoint_commit
        assert _tree_files(repo, tip)["b.txt"] == "written\n"

    async def test_fs_edit_checkpoints(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        repo = make_repo(tmp_path)
        session = Session(str(repo))
        dispatcher = make_dispatcher(repo)
        dispatcher.checkpointer = Checkpointer(repo, session.id)
        monkeypatch.chdir(repo)

        result = await dispatcher.dispatch(
            "c1",
            "fs_edit",
            {"path": "a.txt", "old_string": "base", "new_string": "edited"},
            session=session,
        )

        assert result.status == "success"
        assert result.checkpoint_commit is not None
        tip = _git(repo, "rev-parse", f"refs/heads/tst/session/{session.id}")
        assert _tree_files(repo, tip)["a.txt"] == "edited\n"

    async def test_failed_edit_not_checkpoints(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        session = Session(str(repo))
        dispatcher = make_dispatcher(repo)
        dispatcher.checkpointer = Checkpointer(repo, session.id)
        monkeypatch.chdir(repo)

        result = await dispatcher.dispatch(
            "c1",
            "fs_edit",
            {"path": "a.txt", "old_string": "absent", "new_string": "x"},
            session=session,
        )

        assert result.status == "error"
        assert result.checkpoint_commit is None
        assert _git(repo, "branch", "--list", "tst/session/*") == ""


# ── Loop integration: the diff rides the tool_result event ──────────────


class TestLoopIntegration:
    async def test_tool_result_event_carries_diff(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        session = Session(str(tmp_path))
        router = TierRouter(lead_turns=3)
        config = make_config()
        dispatcher = make_dispatcher(tmp_path)
        registry = dispatcher.registry

        target = tmp_path / "a.txt"
        target.write_text("old line\n")
        # Relative tool arguments from inside the workspace: the loop parses
        # tool arguments as JSON (backslashes in an absolute Windows path
        # break that) and the guard refuses drive-letter absolutes before
        # the write under test (TD-1406).
        monkeypatch.chdir(tmp_path)
        args = json.dumps({"path": "a.txt", "content": "new line\n"})
        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(kind="tool_call", tool_name="fs_write", tool_arguments=args),
                    Script(kind="stream", content="Done"),
                ]
            }
        )

        runner = await start_loop(session, router, mock, config, registry, dispatcher)
        await session.add_user_message("Rewrite the file")
        await wait_for_turn(session, 1)

        events = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
        assert len(events) == 1
        assert events[0].status == "success"
        assert events[0].diff is not None
        assert "-old line" in events[0].diff
        assert "+new line" in events[0].diff

        await runner.cancel()


# ── Registry: fs_edit ships with the built-ins ──────────────────────────


class TestRegistry:
    def test_fs_edit_registered(self) -> None:
        registry = create_registry()
        tool = registry.get("fs_edit")
        assert tool is not None
        assert tool.mutates is True
        assert tool.parallel_safe is False
        assert tool.path_fields == ("path",)
