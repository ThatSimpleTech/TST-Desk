"""A git worktree per session, through the daemon (TD-5126)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.machine_helpers import eventually
from tests.test_artifacts import _open, _shutdown
from tests.test_delegate import _bind_runtime, _parent_dispatcher
from tests.test_two_daemons import _Steps
from tests.test_worktree import git, make_repo, on_disk
from tstd import worktree_session
from tstd.config import WorktreeConfig
from tstd.context.prompt import WORKSPACE_ROOT_LABEL
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.protocol import TurnComplete, WorktreeState
from tstd.session import Session
from tstd.tools.delegate import handle_delegate
from tstd.tools.results import HandlerRefusal
from tstd.worktree import WorktreeError, check_out


async def _send(daemon: Daemon, payload: dict[str, Any]) -> dict[str, Any]:
    raw = await daemon._handle_message(json.dumps(payload), None)
    assert raw is not None
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


async def _rows(daemon: Daemon) -> dict[str, dict[str, Any]]:
    listed = await _send(daemon, {"type": "list_sessions"})
    return {row["session_id"]: row for row in listed["sessions"]}


def _writer(name: str) -> _Steps:
    return _Steps(
        [
            Script(
                kind="tool_call",
                tool_name="fs_write",
                tool_arguments=json.dumps({"path": name, "content": "from the agent\n"}),
            ),
            Script(kind="stream", content="Done."),
        ]
    )


async def _turn(daemon: Daemon, sess: Session, text: str) -> None:
    before = sum(isinstance(e, TurnComplete) for e in sess.event_log.all_events)
    message = {"type": "user_message", "session_id": sess.id, "content": text}
    assert await daemon._handle_message(json.dumps(message), None) is None

    def done() -> bool:
        return sum(isinstance(e, TurnComplete) for e in sess.event_log.all_events) > before

    await eventually(done, within=20)


async def test_a_session_in_a_repo_works_in_its_own_worktree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    daemon = Daemon(data_dir=tmp_path / "data", provider=_writer("out.txt"))
    daemon.skip_all_approvals = True
    sid = await _open(daemon, repo)
    try:
        sess = daemon.session_registry.get(sid)
        assert sess is not None and sess.worktree is not None
        root = Path(sess.workspace_path)
        assert root == tmp_path / "data" / "worktrees" / f"repo-{sid[:8]}"
        assert sess.project_path == str(repo)

        await _turn(daemon, sess, "write it")

        assert (root / "out.txt").read_text(encoding="utf-8") == "from the agent\n"
        assert not (repo / "out.txt").exists()
        row = (await _rows(daemon))[sid]
        # The rail groups by the folder and shows the branch.
        assert row["workspace_path"] == str(repo)
        assert row["worktree"]["branch"] == f"tst/wt/{sid[:8]}"
        assert row["worktree"]["path"] == str(root)
        states = [e for e in sess.event_log.all_events if isinstance(e, WorktreeState)]
        assert states and states[0].worktree is not None
        assert states[0].worktree.branch == f"tst/wt/{sid[:8]}"
    finally:
        await _shutdown(daemon, sid)


async def test_two_sessions_in_one_repo_do_not_share_a_tree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    daemon = Daemon(data_dir=tmp_path / "data")
    first = await _open(daemon, repo)
    try:
        second = (await _send(daemon, {"type": "new_session", "session_id": first}))["session_id"]
        a = daemon.session_registry.get(first)
        b = daemon.session_registry.get(second)
        assert a is not None and b is not None
        assert a.workspace_path != b.workspace_path
        assert b.project_path == str(repo)
        rows = await _rows(daemon)
        assert rows[second]["workspace_path"] == str(repo)
        assert rows[first]["worktree"]["branch"] != rows[second]["worktree"]["branch"]
    finally:
        await _shutdown(daemon, first)


async def test_settings_and_the_workspace_key_turn_it_off(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    daemon = Daemon(data_dir=tmp_path / "data")
    daemon.config = daemon.config.model_copy(update={"worktree": WorktreeConfig(enabled=False)})
    sid = await _open(daemon, repo)
    try:
        sess = daemon.session_registry.get(sid)
        assert sess is not None
        assert sess.worktree is None and sess.workspace_path == str(repo)
        assert (await _rows(daemon))[sid]["worktree"] is None
    finally:
        await _shutdown(daemon, sid)


async def test_the_start_screen_learns_the_main_checkout_is_dirty(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / "README.md").write_text("edited\n", encoding="utf-8")
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, repo)
    try:
        row = (await _rows(daemon))[sid]
        assert row["worktree"]["main_dirty"] == 1
        # "Start in the main checkout instead": the fresh worktree goes.
        listed = await _send(daemon, {"type": "close_worktree", "session_id": sid})
        assert listed["type"] == "session_list"
        sess = daemon.session_registry.get(sid)
        assert sess is not None and sess.worktree is None
        assert sess.workspace_path == str(repo)
        assert not (tmp_path / "data" / "worktrees" / f"repo-{sid[:8]}").exists()
        assert (repo / "README.md").read_text(encoding="utf-8") == "edited\n"
    finally:
        await _shutdown(daemon, sid)


async def test_close_asks_when_there_are_changes_then_merges_back(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    daemon = Daemon(data_dir=tmp_path / "data", provider=_writer("feature.txt"))
    daemon.skip_all_approvals = True
    sid = await _open(daemon, repo)
    try:
        sess = daemon.session_registry.get(sid)
        assert sess is not None
        await _turn(daemon, sess, "write it")

        asked = await _send(daemon, {"type": "close_worktree", "session_id": sid})
        assert asked["type"] == "error"
        assert asked["code"] == "worktree_has_changes"
        merged = await _send(
            daemon, {"type": "close_worktree", "session_id": sid, "action": "merge"}
        )
        assert merged["type"] == "session_list", merged

        assert (repo / "feature.txt").read_text(encoding="utf-8") == "from the agent\n"
        assert sess.worktree is None and sess.workspace_path == str(repo)
        row = next(r for r in merged["sessions"] if r["session_id"] == sid)
        assert row["worktree"] is None
        assert git(repo, "branch", "--list", f"tst/wt/{sid[:8]}") == ""
        note = [e for e in sess.event_log.all_events if isinstance(e, WorktreeState)][-1]
        assert note.worktree is None and "Merged" in note.note
    finally:
        await _shutdown(daemon, sid)


async def test_deleting_a_session_removes_its_unchanged_worktree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, repo)
    sess = daemon.session_registry.get(sid)
    assert sess is not None
    folder = sess.workspace_path
    try:
        listed = await _send(daemon, {"type": "delete_session", "session_id": sid})
        assert listed["type"] == "session_list"
        assert not on_disk(folder)
        assert git(repo, "branch", "--list", f"tst/wt/{sid[:8]}") == ""
    finally:
        await daemon._shutdown()


async def test_a_restart_keeps_the_session_in_its_worktree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, repo)
    sess = daemon.session_registry.get(sid)
    assert sess is not None
    root = sess.workspace_path
    await _shutdown(daemon, sid)

    again = Daemon(data_dir=tmp_path / "data")
    await again._restore_sessions()
    try:
        back = again.session_registry.get(sid)
        assert back is not None
        assert back.workspace_path == root
        assert back.worktree is not None and back.project_path == str(repo)
        row = (await _rows(again))[sid]
        assert row["workspace_path"] == str(repo)
        assert row["worktree"]["branch"] == f"tst/wt/{sid[:8]}"
    finally:
        await again._shutdown()


async def test_a_scheduled_run_that_changes_nothing_leaves_no_worktree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    daemon = Daemon(
        data_dir=tmp_path / "data",
        provider=MockProvider(default=Script(kind="stream", content="Nothing to do.")),
    )
    try:
        result = await daemon._scheduled_run_turn(repo, "check the build")
        assert result.ok, result.summary
        assert result.session_id is not None
        sess = daemon.session_registry.get(result.session_id)
        assert sess is not None
        assert sess.worktree is None and sess.workspace_path == str(repo)
        assert not (tmp_path / "data" / "worktrees" / f"repo-{result.session_id[:8]}").exists()
    finally:
        await daemon._shutdown()


async def test_a_scheduled_run_that_writes_keeps_its_worktree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    daemon = Daemon(data_dir=tmp_path / "data", provider=_writer("report.md"))
    daemon.skip_all_approvals = True
    try:
        result = await daemon._scheduled_run_turn(repo, "write the report")
        assert result.session_id is not None
        sess = daemon.session_registry.get(result.session_id)
        assert sess is not None and sess.worktree is not None
        assert (Path(sess.workspace_path) / "report.md").exists()
        assert not (repo / "report.md").exists()
    finally:
        await daemon._shutdown()


def _delegate_parent(repo: Path, tmp_path: Path, mock: MockProvider) -> Session:
    parent = Session(str(repo))
    parent.worktree_folder = tmp_path / "wts"
    _bind_runtime(parent, _parent_dispatcher(repo), mock)
    return parent


async def test_a_delegate_child_can_work_in_its_own_worktree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    mock = MockProvider(
        sequences={
            "test-worker": [
                Script(
                    kind="tool_call",
                    tool_name="fs_write",
                    tool_arguments='{"path": "child.txt", "content": "child work\\n"}',
                ),
                Script(kind="text", content="Wrote child.txt."),
            ]
        }
    )
    parent = _delegate_parent(repo, tmp_path, mock)

    out = await handle_delegate(parent, "Write child.txt", worktree=True)

    # Not in the parent's files; committed on the child's branch instead.
    assert not (repo / "child.txt").exists()
    branches = git(repo, "branch", "--list", "tst/wt/*").split()
    assert len(branches) == 1
    branch = branches[0]
    assert git(repo, "show", f"{branch}:child.txt") == "child work\n"
    assert branch in out.text and "git merge" in out.text
    assert git(repo, "worktree", "list").count("\n") == 1  # the child's folder is gone
    assert out.transcript and branch in out.transcript[-1].text
    # The child is told its own root, the one its tools are bound to.
    child_root = tmp_path / "wts" / f"repo-{branch.removeprefix('tst/wt/')}"
    system = str(mock.calls[0].messages[0].content)
    assert f"{WORKSPACE_ROOT_LABEL} {child_root.resolve().as_posix()}" in system
    assert f"{WORKSPACE_ROOT_LABEL} {repo.resolve().as_posix()}\n" not in system


async def test_a_delegate_child_can_write_by_the_absolute_root_it_is_told(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    mock = MockProvider(sequences={"test-worker": [Script(kind="text", content="unused")]})
    parent = _delegate_parent(repo, tmp_path, mock)

    class _AbsoluteWriter(MockProvider):
        async def chat_completion(self, request):  # type: ignore[no-untyped-def]
            self.calls.append(request)
            if len(self.calls) > 1:
                return await MockProvider(
                    default=Script(kind="text", content="Wrote.")
                ).chat_completion(request)
            system = str(request.messages[0].content)
            root = system.split(WORKSPACE_ROOT_LABEL, 1)[1].split("\n", 1)[0].strip()
            script = Script(
                kind="tool_call",
                tool_name="fs_write",
                tool_arguments=json.dumps({"path": f"{root}/abs.txt", "content": "abs\n"}),
            )
            return await MockProvider(default=script).chat_completion(request)

    writer = _AbsoluteWriter()
    _bind_runtime(parent, _parent_dispatcher(repo), writer)

    out = await handle_delegate(parent, "Write abs.txt", worktree=True)

    branches = git(repo, "branch", "--list", "tst/wt/*").split()
    assert len(branches) == 1, out.text
    assert git(repo, "show", f"{branches[0]}:abs.txt") == "abs\n"
    assert not (repo / "abs.txt").exists()


async def test_a_delegate_worktree_for_a_folder_the_commit_lacks_is_refused(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    app = repo / "app"
    app.mkdir()
    (app / "main.py").write_text("print(1)\n", encoding="utf-8")
    parent = _delegate_parent(app, tmp_path, MockProvider())
    try:
        await handle_delegate(parent, "Write it", worktree=True)
    except HandlerRefusal as e:
        assert e.code == "delegate_worktree"
        assert "not in the last commit" in e.message
    else:
        raise AssertionError("expected a refusal")


async def test_a_delegate_child_that_changes_nothing_leaves_no_branch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    mock = MockProvider(sequences={"test-worker": [Script(kind="text", content="Nothing.")]})
    parent = _delegate_parent(repo, tmp_path, mock)
    started = time.monotonic()

    out = await handle_delegate(parent, "Look around", worktree="true")

    assert time.monotonic() - started < 30
    assert "changed nothing" in out.text
    assert git(repo, "branch", "--list", "tst/wt/*") == ""


async def test_a_delegate_worktree_outside_git_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    parent = _delegate_parent(plain, tmp_path, MockProvider())
    try:
        await handle_delegate(parent, "Write it", worktree=True)
    except HandlerRefusal as e:
        assert e.code == "delegate_worktree"
    else:
        raise AssertionError("expected a refusal")


async def test_a_worktree_session_moves_only_once_its_worktree_is_closed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    other = tmp_path / "other"
    other.mkdir()
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, repo)
    try:
        move = {"type": "move_session", "session_id": sid, "workspace_path": str(other)}
        refused = await _send(daemon, move)
        assert (refused["type"], refused["code"]) == ("error", "worktree_session")
        await _send(daemon, {"type": "close_worktree", "session_id": sid})
        moved = await _send(daemon, move)
        assert moved["type"] == "session_list"
        sess = daemon.session_registry.get(sid)
        assert sess is not None and sess.workspace_path == str(other)
    finally:
        await _shutdown(daemon, sid)


async def test_a_failed_checkout_moves_the_session_back_to_its_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TD-5187: as when the worktree could not be made at all."""
    repo = make_repo(tmp_path / "repo")

    async def broken(_top: Path, _env: Mapping[str, str]) -> None:
        raise WorktreeError("disk full")

    monkeypatch.setattr(worktree_session, "check_out", broken)
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, repo)
    try:
        sess = daemon.session_registry.get(sid)
        assert sess is not None
        await eventually(lambda: sess.worktree is None, within=10)
        assert sess.workspace_path == str(repo)
        assert (await _rows(daemon))[sid]["worktree"] is None
        assert git(repo, "branch", "--list", f"tst/wt/{sid[:8]}") == ""
        notes = [e.note for e in sess.event_log.all_events if isinstance(e, WorktreeState)]
        assert any("could not be checked out: disk full" in n for n in notes)
    finally:
        await _shutdown(daemon, sid)


async def test_a_restart_finishes_a_checkout_it_cut_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "repo")

    async def stuck(_top: Path, _env: Mapping[str, str]) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(worktree_session, "check_out", stuck)
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, repo)
    sess = daemon.session_registry.get(sid)
    assert sess is not None and sess.worktree is not None
    root = Path(sess.workspace_path)
    assert not (root / "README.md").exists()
    await _shutdown(daemon, sid)
    monkeypatch.undo()

    again = Daemon(data_dir=tmp_path / "data")
    await again._restore_sessions()
    try:
        back = again.session_registry.get(sid)
        assert back is not None and back.worktree is not None
        assert back.worktree.filled
        assert (root / "README.md").read_text(encoding="utf-8") == "hello\n"
        stored = again._session_store.get(sid)
        assert stored is not None and stored.worktree is not None
        assert stored.worktree["filled"] is True
    finally:
        await again._shutdown()


async def test_a_message_while_close_waits_for_the_checkout_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TD-5187: close waits for the checkout; a turn must not start meanwhile."""
    repo = make_repo(tmp_path / "repo")
    gate = asyncio.Event()

    async def slow(top: Path, env: Mapping[str, str]) -> None:
        await gate.wait()
        await check_out(top, env)

    monkeypatch.setattr(worktree_session, "check_out", slow)
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, repo)
    try:
        closing = asyncio.create_task(_send(daemon, {"type": "close_worktree", "session_id": sid}))
        await asyncio.sleep(0.05)
        assert not closing.done()  # waiting, not refused
        message = {"type": "user_message", "session_id": sid, "content": "go"}
        refused = json.loads(await daemon._handle_message(json.dumps(message), None) or "{}")
        assert refused.get("code") == "session_busy"
        gate.set()
        assert (await asyncio.wait_for(closing, 10))["type"] == "session_list"
        sess = daemon.session_registry.get(sid)
        assert sess is not None and sess.folder_busy is None and sess.worktree is None
    finally:
        await _shutdown(daemon, sid)
