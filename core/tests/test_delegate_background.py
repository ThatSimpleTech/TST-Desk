"""TD-5299: ``delegate`` in the background, scoped to the turn.

The call returns before the child's first model call; the parent keeps
working; a settled worker's summary reaches the parent model as a note;
the turn does not end while a worker is alive. Stop ends every worker,
``delegate_stop`` one. A writer always gets its own worktree. Each worker
has its own ceilings and honours the session's caps even under skip-all.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_classifier import WS, boundary, classify, fired_as, req
from tests.test_delegate import _parent_dispatcher, _workspace
from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tests.test_workflow import _parent, _Provider, _repo_parent, _task_of, _text
from tests.test_worktree import git
from tstd.autonomy import DecisionClass
from tstd.config import DelegateBackgroundConfig
from tstd.mock import MockProvider, Script
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.protocol import WorkerJobInfo, WorkerJobs, WorkerResult
from tstd.provider import ChatCompletionRequest
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools.delegate import handle_delegate
from tstd.tools.delegate_background import delegate_output, delegate_stop
from tstd.tools.results import HandlerRefusal
from tstd.worker_restore import restore_workers


def _settings(parent: Session, **over: object) -> None:
    runtime = parent.delegate_runtime
    assert runtime is not None
    runtime.config.delegate.background = DelegateBackgroundConfig(**over)  # type: ignore[attr-defined]


async def _settled(parent: Session, n: int = 1, within: float = 5.0) -> None:
    async def done() -> None:
        while sum(1 for j in parent.workers.all() if not j.live) < n:
            await parent.workers.wait_change()

    await asyncio.wait_for(done(), within)


async def _bg(parent: Session, task: str, **kw: object) -> str:
    out = await handle_delegate(
        parent, task=task, background=True, tool_call_id=f"c-{task[:6]}", **kw
    )
    return out.text


async def test_it_returns_before_the_child_runs_and_its_note_is_owed(tmp_path: Path) -> None:
    hold = asyncio.Event()
    provider = _Provider(lambda _r: _text("Read it: hello."), hold=hold)
    parent = _parent(_workspace(tmp_path), provider)
    text = await _bg(parent, "read notes.md", kind="research", label="read the notes")
    assert text.startswith("Started worker w-1 (read the notes).")
    assert "reads your files as they are" in text
    [job] = parent.workers.all()
    assert job.live and provider.finished == []
    assert parent.workers.owed()
    hold.set()
    await _settled(parent)
    assert job.state == "done" and "hello" in job.summary
    [note] = parent.workers.take_notes()
    assert note.startswith("[Worker w-1 (read the notes) finished.")
    assert not parent.workers.owed()
    results = [e for e in parent.event_log.all_events if isinstance(e, WorkerResult)]
    assert [r.worker_id for r in results] == ["w-1"] and results[0].transcript
    assert any(isinstance(e, WorkerJobs) for e in parent.event_log.all_events)


async def test_more_than_max_running_wait_in_a_queue(tmp_path: Path) -> None:
    hold = asyncio.Event()
    provider = _Provider(lambda _r: _text("done"), hold=hold)
    parent = _parent(_workspace(tmp_path), provider)
    _settings(parent, max_running=1)
    await _bg(parent, "first look", kind="research")
    await _bg(parent, "second look", kind="research")
    await asyncio.sleep(0.05)
    states = [j.state for j in parent.workers.all()]
    assert states == ["running", "queued"]
    hold.set()
    await _settled(parent, 2)
    assert [j.state for j in parent.workers.all()] == ["done", "done"]


async def test_delegate_stop_ends_one_and_its_sibling_runs_on(tmp_path: Path) -> None:
    gate = asyncio.Event()

    def answer(request: ChatCompletionRequest) -> Script:
        return _text("first done" if "first" in _task_of(request) else "second done")

    provider = _Provider(answer, hold=gate)
    parent = _parent(_workspace(tmp_path), provider)
    await _bg(parent, "first look", kind="research")
    await _bg(parent, "second look", kind="research")
    await asyncio.sleep(0.05)
    out = await delegate_stop(session=parent, worker_id="w-1")
    assert "w-1" in out and "stopped" in out
    gate.set()
    await _settled(parent, 2)
    first, second = parent.workers.all()
    assert (first.state, second.state) == ("stopped", "done")
    with pytest.raises(HandlerRefusal) as refused:
        await delegate_stop(session=parent, worker_id="w-1")
    assert refused.value.code == "worker_not_found"
    listed = await delegate_output(session=parent)
    assert "w-1" in listed and "w-2" in listed


async def test_nobody_watching_and_too_many_are_refused(tmp_path: Path) -> None:
    provider = _Provider(lambda _r: _text("x"))
    parent = _parent(_workspace(tmp_path), provider)
    for attribute in ("autonomy", "scheduled_run"):
        setattr(parent, attribute, True)
        with pytest.raises(HandlerRefusal) as refused:
            await _bg(parent, "look", kind="research")
        assert refused.value.code == "delegate_background_unattended"
        assert "foreground" in refused.value.message
        setattr(parent, attribute, False)
    parent.run_permissions = SimpleNamespace(on_ask="deny_and_continue")  # type: ignore[assignment]
    with pytest.raises(HandlerRefusal):
        await _bg(parent, "look", kind="research")
    parent.run_permissions = None
    _settings(parent, max_per_turn=1)
    await _bg(parent, "look", kind="research")
    with pytest.raises(HandlerRefusal) as limit:
        await _bg(parent, "again", kind="research")
    assert limit.value.code == "delegate_background_limit"
    parent.workers.begin_turn()
    await _bg(parent, "next turn", kind="research")
    await _settled(parent, 2)


async def test_a_child_cannot_start_or_read_workers(tmp_path: Path) -> None:
    provider = _Provider(lambda _r: _text("x"))
    parent = _parent(_workspace(tmp_path), provider)
    parent.delegate_depth = 1
    with pytest.raises(HandlerRefusal) as refused:
        await _bg(parent, "look", kind="research")
    assert refused.value.code == "delegate_nested"


def _writer(request: ChatCompletionRequest) -> Script:
    if request.messages[-1].role == "tool":
        return _text("Wrote it.")
    return Script(
        kind="tool_call",
        tool_name="fs_write",
        tool_arguments=json.dumps({"path": "a.txt", "content": "a body\n"}),
    )


async def test_a_writer_always_gets_its_own_worktree(tmp_path: Path) -> None:
    provider = _Provider(_writer)
    repo, parent = _repo_parent(tmp_path, provider)
    text = await _bg(parent, "write a.txt")
    assert "its own worktree on branch" in text
    await _settled(parent)
    [job] = parent.workers.all()
    assert job.state == "done" and job.branch is not None
    assert not (repo / "a.txt").exists()
    assert git(repo, "show", f"{job.branch}:a.txt") == "a body\n"


async def test_a_writer_where_there_can_be_no_worktree_is_told_to_run_in_front(
    tmp_path: Path,
) -> None:
    provider = _Provider(_writer)
    parent = _parent(_workspace(tmp_path), provider)
    with pytest.raises(HandlerRefusal) as refused:
        await _bg(parent, "write a.txt")
    assert "foreground" in refused.value.message
    assert parent.workers.all() == []


def _busy(_request: ChatCompletionRequest) -> Script:
    return Script(kind="tool_call", tool_name="fs_list", tool_arguments=json.dumps({"path": "."}))


async def test_a_worker_stops_at_its_own_call_ceiling(tmp_path: Path) -> None:
    parent = _parent(_workspace(tmp_path), _Provider(_busy))
    _settings(parent, max_iterations=2)
    await _bg(parent, "keep listing", kind="research")
    await _settled(parent)
    [job] = parent.workers.all()
    assert job.state == "stopped" and "delegate.background.max_iterations" in (job.stopped or "")
    assert job.calls == 2


async def test_under_skip_all_a_worker_still_honours_the_session_caps(tmp_path: Path) -> None:
    dispatcher = _parent_dispatcher(_workspace(tmp_path))
    dispatcher.skip_all_fn = lambda: True
    parent = _parent(
        Path(dispatcher.workspace or tmp_path), _Provider(_busy), dispatcher=dispatcher
    )
    parent.boundary_config.caps.max_iterations = 2
    await _bg(parent, "keep listing", kind="research")
    await _settled(parent)
    [job] = parent.workers.all()
    assert job.state == "stopped" and "iteration" in (job.stopped or "")


async def test_a_restart_interrupts_live_workers_once(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    running = WorkerJobInfo(
        worker_id="w-3",
        label="fix it",
        kind="worker",
        task="fix it",
        state="running",
        started_at=1.0,
    )
    done = running.model_copy(update={"worker_id": "w-2", "state": "done"})
    events = [WorkerJobs(session_id="s", workers=[done, running], seq=1)]
    assert await restore_workers(session, events) == 1
    states = {j.id: j.state for j in session.workers.all()}
    assert states == {"w-2": "done", "w-3": "interrupted"}
    [note] = session.workers.take_notes()
    assert "w-3" in note and "interrupted by a restart" in note
    new = session.workers.new(call_id="c", label="", kind="research", task="t", agent=None)
    assert new.id == "w-4"


def test_the_read_and_stop_tools_are_class_a_only_as_builtins() -> None:
    for name in ("delegate_output", "delegate_stop"):
        decision = classify(boundary(), req(tool_name=name))
        assert decision.decision_class is DecisionClass.A and fired_as(decision, "delegate-job")
        for no_grant in (
            req(tool_name=name, provenance="mcp:srv"),
            req(tool_name=name, reads=(WS / "a",)),
            req(tool_name=name, is_mutation=True),
        ):
            assert not fired_as(classify(boundary(), no_grant), "delegate-job")


class _SlowWorker(MockProvider):
    """The child answers only after the parent's next reply is in."""

    async def chat_completion(self, request: ChatCompletionRequest):  # type: ignore[no-untyped-def]
        if request.model == "test-worker":
            await asyncio.sleep(0.4)
        return await super().chat_completion(request)


async def test_the_turn_waits_for_its_worker_and_hears_the_note(tmp_path: Path) -> None:
    """Through the real loop: the parent ends its reply with a worker alive;
    the turn stays open, the note arrives, the model answers, then it ends."""
    ws = _workspace(tmp_path)
    session = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = _SlowWorker(
        sequences={
            "test-brain": [
                Script(
                    kind="tool_call",
                    tool_name="delegate",
                    tool_arguments=json.dumps(
                        {"task": "Read notes.md", "kind": "research", "background": True}
                    ),
                ),
                Script(kind="stream", content="Started it; waiting."),
                Script(kind="stream", content="The worker says hello."),
            ],
            "test-worker": [Script(kind="text", content="notes.md says hello from notes.")],
        }
    )
    runner = await start_loop(
        session,
        TierRouter(),
        mock,
        make_config(),
        registry=dispatcher.registry,
        dispatcher=dispatcher,
    )
    try:
        await session.add_user_message("Look in the background")
        await wait_for_turn(session, 1, _timeout=8.0)
    finally:
        await runner.cancel()
    # The parent was called again after its "waiting" reply, with the note
    # last. The router picks that call's tier, as for any message.
    parent_calls = [c for c in mock.calls if "Read notes.md" not in str(c.messages[1].content)]
    assert len(parent_calls) == 3
    last = parent_calls[-1].messages[-1]
    assert last.role == "user" and "[Worker w-1" in str(last.content)
    assert "hello from notes" in str(last.content)
    replies = [m.content for m in parent_calls[-1].messages if m.role == "assistant"]
    assert "Started it; waiting." in replies
    results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
    assert "Started worker w-1" in results[0].output
    assert [j.state for j in session.workers.all()] == ["done"]


async def test_stop_ends_every_worker_and_leaves_no_worktree_folder(tmp_path: Path) -> None:
    hold = asyncio.Event()
    provider = _Provider(_writer, hold=hold)
    repo, parent = _repo_parent(tmp_path, provider)
    await _bg(parent, "write a.txt")
    await asyncio.sleep(0.05)
    [job] = parent.workers.all()
    folder = Path(job.worktree_path or "")
    assert await asyncio.to_thread(folder.is_dir)
    await parent.workers.stop_all("the turn was stopped")
    assert job.state == "stopped" and job.stopped == "the turn was stopped"
    assert not await asyncio.to_thread(folder.exists)
    assert len(git(repo, "worktree", "list").splitlines()) == 1
