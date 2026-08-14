"""Checkpoint wiring (TD-705) — dispatcher seam and loop integration.

The dispatcher checkpoints successful path-bearing mutations and never
lets a checkpoint failure fail the write; the loop wires a checkpointer
by default and emits one-time ``checkpoint_notice`` events.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_checkpoint import _git, _tree_files, make_repo
from tests.test_dispatch import attach_auto_approver, make_config, start_loop, wait_for_turn
from tstd.autonomy import AmbiguousClassifier, Boundary, Checkpointer, DecisionClassifier
from tstd.autonomy.checkpoint import NO_GIT
from tstd.mock import MockProvider, Script
from tstd.protocol import CheckpointNotice
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import Tool, ToolDispatcher, ToolRegistry
from tstd.tools.boundary import PathGuard

# ── Helpers ─────────────────────────────────────────────────────────────


async def _stub_worker(prompt: str) -> str:
    """Stub worker-tier classifier: always answers B (fail toward asking)."""
    return "B"


def make_write_tool(name: str = "fs_write", mutates: bool = True) -> Tool:
    return Tool(
        name=name,
        description=f"{name} test tool",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        side_effect_class="auto",
        parallel_safe=False,
        path_fields=("path",),
        host_fields=(),
        mutates=mutates,
    )


def _write_file(path: str, content: str) -> None:
    """Sync file write helper — avoids Path I/O inside async test handlers."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _read_file(path: str) -> str:
    return Path(path).read_text()


async def _write_handler(session: object, path: str, content: str, tool_call_id: str = "") -> str:
    _write_file(path, content)
    return f"wrote {len(content)} chars"


def make_dispatcher(workspace: Path, registry: ToolRegistry) -> ToolDispatcher:
    boundary = Boundary(workspace_root=workspace)
    return attach_auto_approver(  # TD-802: mechanics tests auto-approve
        ToolDispatcher(
            registry,
            classifier=AmbiguousClassifier(
                static=DecisionClassifier(boundary),
                call_worker=_stub_worker,
            ),
            path_guard=PathGuard(boundary),
        )
    )


def make_write_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(make_write_tool())
    return registry


# ── Dispatcher seam ─────────────────────────────────────────────────────


class TestDispatcherSeam:
    # Every test dispatches workspace-relative paths from inside the
    # workspace: the guard refuses drive-letter absolutes as windows_unsafe
    # before any workspace logic runs (TD-1406), and tmp_path is always a
    # drive-letter path on Windows.

    async def test_mutating_tool_checkpoints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        session = Session(str(repo))
        dispatcher = make_dispatcher(repo, make_write_registry())
        dispatcher.register_handler("fs_write", _write_handler)
        dispatcher.checkpointer = Checkpointer(repo, session.id)
        monkeypatch.chdir(repo)

        result = await dispatcher.dispatch(
            "c1", "fs_write", {"path": "b.txt", "content": "hello\n"}, session=session
        )

        assert result.status == "success"
        assert result.checkpoint_commit is not None
        tip = _git(repo, "rev-parse", f"refs/heads/tst/session/{session.id}")
        assert tip == result.checkpoint_commit
        files = _tree_files(repo, tip)
        assert files["b.txt"] == "hello\n"
        assert files["a.txt"] == "base\n"  # baseline carried forward

    async def test_read_tool_does_not_checkpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        session = Session(str(repo))
        registry = ToolRegistry()
        registry.register(make_write_tool(name="fs_read", mutates=False))
        dispatcher = make_dispatcher(repo, registry)

        async def read_handler(
            session: object, path: str, content: str, tool_call_id: str = ""
        ) -> str:
            return _read_file(path)

        dispatcher.register_handler("fs_read", read_handler)
        dispatcher.checkpointer = Checkpointer(repo, session.id)
        monkeypatch.chdir(repo)

        result = await dispatcher.dispatch(
            "c1", "fs_read", {"path": "a.txt", "content": ""}, session=session
        )

        assert result.status == "success"
        assert result.checkpoint_commit is None
        assert _git(repo, "branch", "--list", "tst/session/*") == ""

    async def test_no_session_skips_checkpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        dispatcher = make_dispatcher(repo, make_write_registry())
        dispatcher.register_handler("fs_write", _write_handler)
        dispatcher.checkpointer = Checkpointer(repo, "detached")
        monkeypatch.chdir(repo)

        result = await dispatcher.dispatch("c1", "fs_write", {"path": "b.txt", "content": "x\n"})

        assert result.status == "success"  # the write still happened
        assert result.checkpoint_commit is None
        assert _git(repo, "branch", "--list", "tst/session/*") == ""

    async def test_failed_handler_not_checkpoints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        session = Session(str(repo))
        dispatcher = make_dispatcher(repo, make_write_registry())

        async def failing_handler(
            session: object, path: str, content: str, tool_call_id: str = ""
        ) -> str:
            raise OSError("disk on fire")

        dispatcher.register_handler("fs_write", failing_handler)
        dispatcher.checkpointer = Checkpointer(repo, session.id)
        monkeypatch.chdir(repo)

        result = await dispatcher.dispatch(
            "c1", "fs_write", {"path": "b.txt", "content": "x\n"}, session=session
        )

        assert result.status == "error"
        assert result.checkpoint_commit is None
        assert _git(repo, "branch", "--list", "tst/session/*") == ""

    async def test_no_git_notice_flows_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not a git repo: the write succeeds, the notice rides the result.
        session = Session(str(tmp_path))
        dispatcher = make_dispatcher(tmp_path, make_write_registry())
        dispatcher.register_handler("fs_write", _write_handler)
        dispatcher.checkpointer = Checkpointer(tmp_path, session.id)
        monkeypatch.chdir(tmp_path)

        result = await dispatcher.dispatch(
            "c1", "fs_write", {"path": "b.txt", "content": "x\n"}, session=session
        )

        assert result.status == "success"
        assert result.checkpoint_commit is None
        assert result.checkpoint_notice is not None
        assert result.checkpoint_notice.code == NO_GIT

    async def test_checkpoint_failure_does_not_fail_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        session = Session(str(repo))
        dispatcher = make_dispatcher(repo, make_write_registry())
        dispatcher.register_handler("fs_write", _write_handler)

        class ExplodingCheckpointer:
            async def checkpoint(self, *args: object, **kwargs: object) -> object:
                raise RuntimeError("boom")

        dispatcher.checkpointer = ExplodingCheckpointer()  # type: ignore[assignment]
        monkeypatch.chdir(repo)

        result = await dispatcher.dispatch(
            "c1", "fs_write", {"path": "b.txt", "content": "x\n"}, session=session
        )

        assert result.status == "success"  # the write survives its checkpoint
        assert result.checkpoint_commit is None


# ── Loop integration ────────────────────────────────────────────────────


class TestLoopIntegration:
    # Relative tool arguments from inside the workspace: the loop parses
    # tool arguments as JSON (backslashes in an absolute Windows path break
    # that) and the guard refuses drive-letter absolutes before the write
    # under test (TD-1406).

    async def test_loop_wires_checkpointer_and_commits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """agent_loop wires a checkpointer; a write lands on the session branch."""
        repo = make_repo(tmp_path)
        session = Session(str(repo))
        router = TierRouter(lead_turns=3)
        config = make_config()
        registry = make_write_registry()
        dispatcher = make_dispatcher(repo, registry)
        dispatcher.register_handler("fs_write", _write_handler)
        assert dispatcher.checkpointer is None  # the loop must wire one

        monkeypatch.chdir(repo)
        args = json.dumps({"path": "b.txt", "content": "loop write\n"})
        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(kind="tool_call", tool_name="fs_write", tool_arguments=args),
                    Script(kind="stream", content="Done"),
                ]
            }
        )

        runner = await start_loop(session, router, mock, config, registry, dispatcher)
        await session.add_user_message("Write a file")
        await wait_for_turn(session, 1)

        tool_results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
        assert len(tool_results) == 1
        assert tool_results[0].status == "success"

        branch = f"refs/heads/tst/session/{session.id}"
        tip = _git(repo, "rev-parse", branch)
        files = _tree_files(repo, tip)
        assert files["b.txt"] == "loop write\n"
        message = _git(repo, "log", "-1", "--format=%B", tip)
        assert session.id in message  # commit references the session

        await runner.cancel()

    async def test_loop_emits_checkpoint_notice_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two writes in a non-git workspace emit exactly one notice event."""
        session = Session(str(tmp_path))
        router = TierRouter(lead_turns=3)
        config = make_config()
        registry = make_write_registry()
        dispatcher = make_dispatcher(tmp_path, registry)
        dispatcher.register_handler("fs_write", _write_handler)
        monkeypatch.chdir(tmp_path)

        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="fs_write",
                        tool_arguments=json.dumps({"path": "one.txt", "content": "1"}),
                    ),
                    Script(
                        kind="tool_call",
                        tool_name="fs_write",
                        tool_arguments=json.dumps({"path": "two.txt", "content": "2"}),
                    ),
                    Script(kind="stream", content="Done"),
                ]
            }
        )

        runner = await start_loop(session, router, mock, config, registry, dispatcher)
        await session.add_user_message("Write two files")
        await wait_for_turn(session, 1)

        notices = [e for e in session.event_log.all_events if isinstance(e, CheckpointNotice)]
        assert len(notices) == 1
        assert notices[0].code == NO_GIT
        assert notices[0].session_id == session.id
        # Both writes still happened.
        assert (tmp_path / "one.txt").read_text() == "1"
        assert (tmp_path / "two.txt").read_text() == "2"

        await runner.cancel()
