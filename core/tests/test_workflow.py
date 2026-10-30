"""The workflow tool: workers side by side, each result checked (TD-5195)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.test_delegate import _bind_runtime, _parent_dispatcher, _workspace
from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tests.test_worktree import git, make_repo
from tstd.autonomy import Boundary, DecisionClassifier
from tstd.boundary_config import CapsSection
from tstd.config import WorkflowConfig
from tstd.mock import MockProvider, Script
from tstd.policy import ApprovalOutcome
from tstd.provider import ChatCompletionRequest, ChatCompletionResponse, ProviderError
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import create_registry
from tstd.tools.delegate import DelegateRuntime, run_worker_child
from tstd.tools.dispatch import ToolDispatcher, build_decision_request
from tstd.tools.results import HandlerOutput, HandlerRefusal
from tstd.tools.workflow import ULTRACODE_NOTE, handle_workflow, parse_verdict

Answer = Callable[[ChatCompletionRequest], Script]


def _task_of(request: ChatCompletionRequest) -> str:
    """The user message a child was given: its preamble and task."""
    return str(request.messages[1].content)


def _answered(request: ChatCompletionRequest) -> bool:
    """Whether the child's last message is a tool result."""
    return request.messages[-1].role == "tool"


class _Provider:
    """Answers each child by its request; can hold calls until *gate* are in flight."""

    def __init__(
        self,
        answer: Answer,
        *,
        gate: int = 0,
        delay: Callable[[ChatCompletionRequest], float] | None = None,
        hold: asyncio.Event | None = None,
    ) -> None:
        self.answer = answer
        self.gate = gate
        self.delay = delay
        self.hold = hold
        self.calls: list[ChatCompletionRequest] = []
        self.finished: list[str] = []
        self.in_flight = 0
        self.peak = 0
        self._open = asyncio.Event()

    async def chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse | ProviderError:
        self.calls.append(request)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self.in_flight >= self.gate:
                self._open.set()
            # Held until *gate* calls are in flight at once: calls that ran
            # one after another would time out here.
            await asyncio.wait_for(self._open.wait(), 5)
            if self.hold is not None:
                await self.hold.wait()
            await asyncio.sleep(self.delay(request) if self.delay else 0.01)
            script = self.answer(request)
            return await MockProvider(default=script).chat_completion(request)
        finally:
            self.in_flight -= 1
            self.finished.append(_task_of(request))

    def models(self) -> list[str]:
        return [c.model for c in self.calls]


def _text(content: str) -> Script:
    return Script(kind="text", content=content)


def _worker_then_checker(worker: Answer, checker: Answer | None = None) -> Answer:
    def answer(request: ChatCompletionRequest) -> Script:
        if request.model == "test-validator" and checker is not None:
            return checker(request)
        return worker(request)

    return answer


def _parent(
    ws: Path,
    provider: object,
    *,
    on: bool = True,
    max_parallel: int = 3,
    max_tasks: int = 8,
    dispatcher: ToolDispatcher | None = None,
) -> Session:
    parent = Session(str(ws))
    router = TierRouter()
    router.set_ultracode(on)
    parent.router = router
    runtime = _bind_runtime(parent, dispatcher or _parent_dispatcher(ws), provider)  # type: ignore[arg-type]
    runtime.config.workflow = WorkflowConfig(max_parallel=max_parallel, max_tasks=max_tasks)
    return parent


def _runtime(parent: Session) -> DelegateRuntime:
    runtime = parent.delegate_runtime
    assert isinstance(runtime, DelegateRuntime)
    return runtime


def _tasks(*texts: str, **extra: Any) -> list[dict[str, Any]]:
    return [{"task": t, **extra} for t in texts]


def _research(*texts: str) -> list[dict[str, Any]]:
    """Read-only tasks: two or more writers would each need a worktree."""
    return _tasks(*texts, kind="research")


def _sections(out: HandlerOutput) -> list[str]:
    return [line for line in out.text.splitlines() if line.startswith("## ")]


# ── Refusals ────────────────────────────────────────────────────────────


async def test_refused_while_ultracode_is_off(tmp_path: Path) -> None:
    provider = _Provider(lambda _r: _text("never"))
    parent = _parent(_workspace(tmp_path), provider, on=False)

    with pytest.raises(HandlerRefusal) as refused:
        await handle_workflow(parent, _tasks("read notes.md"))

    assert refused.value.code == "workflow_off"
    assert "Ultracode is off" in refused.value.message
    assert "Turn on Ultracode in the model picker" in refused.value.message
    assert "delegate" in refused.value.message
    assert provider.calls == []


async def test_refused_inside_a_worker(tmp_path: Path) -> None:
    provider = _Provider(lambda _r: _text("never"))
    parent = _parent(_workspace(tmp_path), provider)
    parent.delegate_depth = 1

    with pytest.raises(HandlerRefusal) as refused:
        await handle_workflow(parent, _tasks("read notes.md"))

    assert refused.value.code == "workflow_nested"
    assert provider.calls == []


async def test_too_many_tasks_are_refused(tmp_path: Path) -> None:
    provider = _Provider(lambda _r: _text("never"))
    parent = _parent(_workspace(tmp_path), provider, max_tasks=2)

    with pytest.raises(HandlerRefusal) as refused:
        await handle_workflow(parent, _tasks("a", "b", "c"))

    assert refused.value.code == "workflow_too_many"
    assert "at most 2" in refused.value.message
    assert provider.calls == []


async def test_a_refused_call_through_dispatch_reports_the_fix(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    dispatcher = _parent_dispatcher(ws)
    parent = _parent(ws, _Provider(lambda _r: _text("never")), on=False, dispatcher=dispatcher)

    result = await dispatcher.dispatch("c1", "workflow", {"tasks": _tasks("x")}, parent)

    assert result.status == "error"
    assert result.error_code == "workflow_off"


# ── Running side by side ────────────────────────────────────────────────


async def test_tasks_run_at_the_same_time(tmp_path: Path) -> None:
    provider = _Provider(lambda _r: _text("finished"), gate=3)
    parent = _parent(_workspace(tmp_path), provider, max_parallel=3)

    out = await handle_workflow(parent, _research("alpha", "beta", "gamma"), check=False)

    assert provider.peak == 3
    assert out.text.startswith("Workflow: 3 tasks, 3 done. Checks: off.")
    assert all("[done]" in line for line in _sections(out))


async def test_never_more_than_max_parallel_at_once(tmp_path: Path) -> None:
    provider = _Provider(lambda _r: _text("finished"), gate=2)
    parent = _parent(_workspace(tmp_path), provider, max_parallel=2)

    out = await handle_workflow(parent, _research("a1", "a2", "a3", "a4", "a5"))

    # Workers and their checks share the slots.
    assert provider.peak == 2
    assert provider.models().count("test-worker") == 5
    assert provider.models().count("test-validator") == 5
    assert "5 done" in out.text


async def test_results_come_back_in_task_order(tmp_path: Path) -> None:
    delays = {"alpha": 0.3, "beta": 0.15, "gamma": 0.0}

    def delay(request: ChatCompletionRequest) -> float:
        return next(d for name, d in delays.items() if name in _task_of(request))

    def answer(request: ChatCompletionRequest) -> Script:
        name = next(n for n in delays if n in _task_of(request))
        return _text(f"summary of {name}")

    provider = _Provider(answer, delay=delay)
    parent = _parent(_workspace(tmp_path), provider)

    out = await handle_workflow(
        parent,
        [
            {"task": "alpha task", "label": "First", "kind": "research"},
            {"task": "beta task", "kind": "research"},
            {"task": "gamma task", "kind": "research"},
        ],
        check=False,
    )

    finished = [next(n for n in delays if n in text) for text in provider.finished]
    assert finished == ["gamma", "beta", "alpha"]
    assert _sections(out) == [
        "## 1. First  [done]",
        "## 2. beta task  [done]",
        "## 3. gamma task  [done]",
    ]
    assert out.text.index("summary of alpha") < out.text.index("summary of gamma")
    # The window gets each worker's steps under its own header.
    notes = [s.text for s in out.transcript if s.kind == "note"]
    assert notes[:3] == ["Task 1: First", "Task 2: beta task", "Task 3: gamma task"]


# ── Plan mode ───────────────────────────────────────────────────────────


def _planning(arguments: dict[str, Any]) -> bool:
    tool = create_registry().require("workflow")
    boundary = Boundary(workspace_root=Path("/work/proj"), plan_mode=lambda: True)
    return DecisionClassifier(boundary).classify(build_decision_request(tool, arguments)).refused


async def test_plan_mode_refuses_a_worker_task_and_runs_research(tmp_path: Path) -> None:
    assert _planning({"tasks": [{"task": "fix it", "kind": "worker"}]})
    assert _planning(
        {"tasks": [{"task": "a", "kind": "research"}, {"task": "b", "kind": "worker"}]}
    )
    assert not _planning({"tasks": [{"task": "find callers"}, {"task": "b", "kind": "research"}]})

    provider = _Provider(lambda _r: _text("found it"))
    parent = _parent(_workspace(tmp_path), provider)
    assert parent.router is not None
    parent.router.set_plan(True)

    out = await handle_workflow(
        parent, [{"task": "find callers"}, {"task": "read docs", "kind": "research"}], check=False
    )

    assert "2 done" in out.text
    for call in provider.calls:
        names = {t.function.name for t in call.tools or [] if t.function is not None}
        assert "web_search" in names
        assert not names & {"fs_write", "fs_edit", "shell", "delegate", "workflow"}


# ── Writers in worktrees ────────────────────────────────────────────────


def _writer(request: ChatCompletionRequest) -> Script:
    if _answered(request):
        return _text("Wrote it.")
    name = "a.txt" if "write a.txt" in _task_of(request) else "b.txt"
    return Script(
        kind="tool_call",
        tool_name="fs_write",
        tool_arguments=json.dumps({"path": name, "content": f"{name} body\n"}),
    )


def _repo_parent(tmp_path: Path, provider: object, **kw: Any) -> tuple[Path, Session]:
    repo = make_repo(tmp_path / "repo")
    parent = _parent(repo, provider, **kw)
    parent.worktree_folder = tmp_path / "wts"
    return repo, parent


async def test_two_writers_each_get_their_own_worktree(tmp_path: Path) -> None:
    checker_tasks: list[str] = []

    def checker(request: ChatCompletionRequest) -> Script:
        checker_tasks.append(_task_of(request))
        return _text("VERDICT: confirmed\nThe file is on the branch.")

    provider = _Provider(_worker_then_checker(_writer, checker))
    repo, parent = _repo_parent(tmp_path, provider)

    out = await handle_workflow(parent, _tasks("write a.txt", "write b.txt"))

    assert not (repo / "a.txt").exists() and not (repo / "b.txt").exists()
    branches = git(repo, "branch", "--list", "tst/wt/*").split()
    assert len(branches) == 2
    shown = {
        b: git(repo, "show", f"{b}:{n}")
        for b in branches
        for n in ("a.txt", "b.txt")
        if n in git(repo, "ls-tree", "--name-only", b)
    }
    assert sorted(shown.values()) == ["a.txt body\n", "b.txt body\n"]
    assert all(b in out.text for b in branches)
    assert "Checks: 2 confirmed." in out.text
    # Each reviewer is shown that writer's branch and its diff.
    assert len(checker_tasks) == 2
    assert all("```diff" in t and "tst/wt/" in t for t in checker_tasks)


async def test_plan_mode_turned_on_mid_run_stops_worktree_writers(tmp_path: Path) -> None:
    # A worktree child has no router of its own; its classifier must read the
    # parent's plan mode, or a writer keeps writing after planning starts.
    parent_box: list[Session] = []

    def writer(request: ChatCompletionRequest) -> Script:
        if not _answered(request):
            router = parent_box[0].router
            assert router is not None
            router.set_plan(True)
        return _writer(request)

    provider = _Provider(writer)
    repo, parent = _repo_parent(tmp_path, provider)
    parent_box.append(parent)

    await handle_workflow(parent, _tasks("write a.txt", "write b.txt"), check=False)

    assert git(repo, "branch", "--list", "tst/wt/*") == ""
    assert not (repo / "a.txt").exists() and not (repo / "b.txt").exists()


async def test_one_task_works_in_the_parents_files(tmp_path: Path) -> None:
    provider = _Provider(_writer)
    repo, parent = _repo_parent(tmp_path, provider)

    await handle_workflow(parent, _tasks("write a.txt"), check=False)

    assert (repo / "a.txt").read_text(encoding="utf-8") == "a.txt body\n"
    assert git(repo, "branch", "--list", "tst/wt/*") == ""


async def test_childrens_call_ids_are_prefixed_so_approvals_do_not_collide(
    tmp_path: Path,
) -> None:
    asked: list[str] = []
    both = asyncio.Event()

    async def approve(tool_call_id: str, *_args: object) -> ApprovalOutcome:
        asked.append(tool_call_id)
        if len(asked) == 2:
            both.set()
        # Both children park at once, as they would on the parent's cards.
        await asyncio.wait_for(both.wait(), 5)
        return ApprovalOutcome(False, "denied in the test")

    def worker(request: ChatCompletionRequest) -> Script:
        if _answered(request):
            return _text("Could not run it.")
        return Script(kind="tool_call", tool_name="shell", tool_arguments='{"command": "make"}')

    provider = _Provider(worker)
    repo = make_repo(tmp_path / "repo")
    dispatcher = _parent_dispatcher(repo)
    dispatcher.approval_handler = approve
    parent = _parent(repo, provider, dispatcher=dispatcher)
    parent.worktree_folder = tmp_path / "wts"

    await handle_workflow(parent, _tasks("build one", "build two"), check=False, tool_call_id="wf9")

    assert sorted(asked) == ["wf9.1/call_mock_1", "wf9.2/call_mock_1"]
    # Each child's model is answered under the id it gave.
    replies = [m for c in provider.calls for m in c.messages if m.role == "tool"]
    assert replies and {m.tool_call_id for m in replies} == {"call_mock_1"}


async def test_a_cancelled_approval_still_settles_every_writers_worktree(
    tmp_path: Path,
) -> None:
    # Session.cancel cancels the approval futures the children wait on. The
    # CancelledError must not skip the worktree settle, and the workflow must
    # not return while a sibling is still running.
    waiting: list[str] = []
    both = asyncio.Event()

    async def approve(tool_call_id: str, *_args: object) -> ApprovalOutcome:
        waiting.append(tool_call_id)
        if len(waiting) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)
        raise asyncio.CancelledError

    def worker(request: ChatCompletionRequest) -> Script:
        tool_results = sum(m.role == "tool" for m in request.messages)
        name = "a.txt" if "write a.txt" in _task_of(request) else "b.txt"
        if tool_results == 0:
            return Script(
                kind="tool_call",
                tool_name="fs_write",
                tool_arguments=json.dumps({"path": name, "content": f"{name} body\n"}),
            )
        if tool_results == 1:
            return Script(kind="tool_call", tool_name="shell", tool_arguments='{"command": "make"}')
        return _text("done")

    provider = _Provider(worker)
    repo = make_repo(tmp_path / "repo")
    dispatcher = _parent_dispatcher(repo)
    dispatcher.approval_handler = approve
    parent = _parent(repo, provider, dispatcher=dispatcher)
    parent.worktree_folder = tmp_path / "wts"

    with pytest.raises(asyncio.CancelledError):
        await handle_workflow(parent, _tasks("write a.txt", "write b.txt"), check=False)

    assert len(waiting) == 2
    # No worktree folder is left, and each writer's file is kept on its branch.
    leftover = (
        [p for p in (tmp_path / "wts").rglob("*") if p.is_dir()]
        if (tmp_path / "wts").exists()
        else []
    )
    assert leftover == []
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    branches = git(repo, "branch", "--list", "tst/wt/*").split()
    assert len(branches) == 2


# ── The check stage ─────────────────────────────────────────────────────


async def test_each_finished_task_is_checked_and_failed_ones_are_not(tmp_path: Path) -> None:
    verdicts = {
        "alpha": "VERDICT: confirmed\nRead the file; it says so.",
        "beta": "**VERDICT:** wrong — the function does not exist\nNo such name in src.",
        "gamma": "Looks fine to me, probably.",
    }

    def worker(request: ChatCompletionRequest) -> Script:
        if "delta" in _task_of(request):
            return Script(kind="error", content="the model is down", status_code=500)
        return _text(f"worker summary for {_task_of(request).split('Task:')[-1].strip()}")

    def checker(request: ChatCompletionRequest) -> Script:
        return _text(next(v for k, v in verdicts.items() if k in _task_of(request)))

    provider = _Provider(_worker_then_checker(worker, checker))
    parent = _parent(_workspace(tmp_path), provider)

    out = await handle_workflow(parent, _research("alpha", "beta", "gamma", "delta"))

    assert out.text.startswith(
        "Workflow: 4 tasks, 3 done, 1 failed. Checks: 1 confirmed, 1 doubtful, 1 wrong."
    )
    sections = _sections(out)
    assert sections[0] == "## 1. alpha  [done] [check: confirmed]"
    assert sections[1] == "## 2. beta  [done] [check: wrong]"
    assert sections[2] == "## 3. gamma  [done] [check: doubtful]"
    assert sections[3].startswith("## 4. delta  [failed: ")
    assert "check:" not in sections[3]
    assert "Check: the function does not exist\nNo such name in src." in out.text
    assert "The checker gave no verdict." in out.text
    checked = [_task_of(c) for c in provider.calls if c.model == "test-validator"]
    assert len(checked) == 3 and not any("delta" in t for t in checked)
    # The reviewer is read-only, and its cost rows say what they were.
    for call in (c for c in provider.calls if c.model == "test-validator"):
        names = {t.function.name for t in call.tools or [] if t.function is not None}
        assert "fs_write" not in names and "shell" not in names
    tracker = parent.cost_tracker
    assert tracker is not None
    assert {c.source for c in tracker.calls if c.tier == "validator"} == {"check"}
    assert {c.source for c in tracker.calls if c.tier == "worker"} == {"worker"}


async def test_the_check_falls_back_to_the_worker_tier(tmp_path: Path) -> None:
    provider = _Provider(lambda _r: _text("VERDICT: confirmed"))
    parent = _parent(_workspace(tmp_path), provider)
    _runtime(parent).config.presets["test"].validator.slug = None

    out = await handle_workflow(parent, _tasks("alpha"))

    assert provider.models() == ["test-worker", "test-worker"]
    assert "[check: confirmed]" in out.text


@pytest.mark.parametrize(
    ("reply", "verdict"),
    [
        ("VERDICT: confirmed", "confirmed"),
        ("verdict: Doubtful\nnot sure", "doubtful"),
        ("Summary first.\nVERDICT: wrong\nreason", "wrong"),
        ("**VERDICT:** confirmed", "confirmed"),
        ("**VERDICT**: wrong\nno such function", "wrong"),
        ("Verdict: `wrong`", "wrong"),
        ("VERDICT: confirmed | doubtful | wrong", "doubtful"),
        ("", "doubtful"),
        ("I could not decide.", "doubtful"),
    ],
)
def test_verdicts_are_parsed_and_junk_is_doubtful(reply: str, verdict: str) -> None:
    assert parse_verdict(reply)[0] == verdict


def test_reasons_are_at_most_five_lines() -> None:
    _, reasons = parse_verdict("VERDICT: wrong\n" + "\n".join(f"r{i}" for i in range(9)))
    assert reasons.splitlines() == ["r0", "r1", "r2", "r3", "r4"]


# ── Stop, caps and effort ───────────────────────────────────────────────


async def _running(session: Session) -> None:
    await session.set_state("running")
    await session.add_user_message("go")


async def test_stop_reaches_running_children(tmp_path: Path) -> None:
    hold = asyncio.Event()  # never set: the model calls hang until Stop
    provider = _Provider(lambda _r: _text("never"), hold=hold)
    parent = _parent(_workspace(tmp_path), provider, max_parallel=2)
    await _running(parent)

    run = asyncio.create_task(handle_workflow(parent, _research("a1", "a2", "a3")))
    for _ in range(200):
        if provider.in_flight == 2:
            break
        await asyncio.sleep(0.01)
    assert provider.in_flight == 2
    stopped_at = time.monotonic()
    await parent.stop_turn()
    out = await asyncio.wait_for(run, 3)

    assert time.monotonic() - stopped_at < 2
    sections = _sections(out)
    assert sections[0] == "## 1. a1  [failed: parent stopped the turn]"
    assert sections[1] == "## 2. a2  [failed: parent stopped the turn]"
    assert sections[2] == "## 3. a3  [failed: the turn was stopped before it started]"
    assert provider.models() == ["test-worker", "test-worker"]


async def test_stop_reaches_a_childs_shell(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    marker = ws / "started.txt"

    def worker(request: ChatCompletionRequest) -> Script:
        if _answered(request):
            return _text("The command was stopped.")
        command = json.dumps({"command": f"touch {marker} && sleep 30"})
        return Script(kind="tool_call", tool_name="shell", tool_arguments=command)

    provider = _Provider(worker)
    parent = _parent(ws, provider)
    await _running(parent)

    run = asyncio.create_task(handle_workflow(parent, _tasks("run the slow thing"), check=False))
    for _ in range(500):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    assert marker.exists()
    await parent.stop_turn()
    out = await asyncio.wait_for(run, 5)

    results = [s for s in out.transcript if s.kind == "tool_result" and s.tool == "shell"]
    assert results and "stopped" in results[0].text


async def test_children_share_one_iteration_count(tmp_path: Path) -> None:
    def reader(_request: ChatCompletionRequest) -> Script:
        return Script(kind="tool_call", tool_name="fs_read", tool_arguments='{"path": "notes.md"}')

    provider = _Provider(reader)
    parent = _parent(_workspace(tmp_path), provider, max_parallel=3)
    parent.boundary_config.caps = CapsSection(max_iterations=5)
    _runtime(parent).iterations = 1

    out = await handle_workflow(parent, _research("a1", "a2", "a3"), check=False)

    # Three children, not 3 x 4 calls: the parent's one call plus theirs
    # stay under the cap together.
    assert provider.models().count("test-worker") == 4
    assert _runtime(parent).child_calls == 4
    stopped = [s for s in _sections(out) if "[failed: iteration cap exceeded: 5 >= 5]" in s]
    assert len(stopped) == 3


async def test_children_run_at_the_sessions_effort_when_the_tier_takes_it(
    tmp_path: Path,
) -> None:
    provider = _Provider(lambda _r: _text("VERDICT: confirmed"))
    parent = _parent(_workspace(tmp_path), provider)
    preset = _runtime(parent).config.presets["test"]
    preset.worker.reasoning_effort = True
    preset.worker.extra_body = {"keep": 1}
    assert parent.router is not None
    parent.router.set_effort("high")

    await handle_workflow(parent, _tasks("alpha"))
    await run_worker_child(parent=parent, task="plain delegate")

    by_model = {c.model: c.extra_body for c in provider.calls}
    assert by_model["test-worker"] == {"keep": 1, "reasoning_effort": "high"}
    # The validator tier does not opt in, so its checks carry no effort.
    assert by_model["test-validator"] is None
    assert all(
        c.extra_body == {"keep": 1, "reasoning_effort": "high"}
        for c in provider.calls
        if c.model == "test-worker"
    )


# ── The turn: one tool list, a note on the message ─────────────────────


async def test_the_tool_list_is_the_same_on_and_off_and_the_note_rides_on_the_message(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path)
    dispatcher = _parent_dispatcher(ws)
    session = Session(str(ws))
    router = TierRouter()
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(kind="stream", content="one"),
                Script(kind="stream", content="two"),
            ]
        }
    )
    runner = await start_loop(
        session, router, mock, make_config(), registry=dispatcher.registry, dispatcher=dispatcher
    )
    try:
        await session.add_user_message("first")
        await wait_for_turn(session, 1)
        router.set_ultracode(True)
        await session.add_user_message("second")
        await wait_for_turn(session, 2)
    finally:
        await runner.cancel()

    off, on = [c for c in mock.calls if c.model == "test-brain"]
    assert off.tools == on.tools
    assert "workflow" in {t.function.name for t in on.tools or [] if t.function is not None}
    assert ULTRACODE_NOTE not in str(off.messages[-1].content)
    assert str(on.messages[-1].content).endswith(ULTRACODE_NOTE)
    assert str(on.messages[-1].content).startswith("second")
    for call in (off, on):
        assert ULTRACODE_NOTE not in str(call.messages[0].content)
        assert sum(ULTRACODE_NOTE in str(m.content) for m in call.messages) == (call is on)
