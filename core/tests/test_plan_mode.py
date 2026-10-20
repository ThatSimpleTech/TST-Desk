"""TD-5123: read-only plan mode.

While a session is planning, the classifier refuses every call that could
change something (``fs_write``, ``fs_edit``, ``shell`` outside the TD-5103
list, MCP tools not marked read-only), ahead of policy rules and skip-all,
and the refusal tells the model it is planning. Plan mode ends with
``exit_plan_mode``: the person approves, edits or rejects the plan, and
approving turns plan mode off with the plan pinned.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

import tstd.shell_read_only as shell_read_only
from tests.machine_helpers import eventually
from tests.test_artifacts import _open, _shutdown
from tests.test_two_daemons import _Steps
from tstd import peer_policy
from tstd.autonomy.classifier import (
    RULE_TABLE,
    Boundary,
    Classification,
    DecisionClass,
    DecisionClassifier,
)
from tstd.daemon import Daemon
from tstd.mock import Script
from tstd.notify import discord, ntfy, slack, telegram
from tstd.plan_mode import PLAN_NOTE, with_plan_note
from tstd.plan_review import (
    carry_for,
    keep_pinned_plan_visible,
    restore_pinned_plan,
    restored_plan_mode,
)
from tstd.policy import PolicyConfig, PolicyRule
from tstd.protocol import (
    ApprovalRequest,
    PlanPinned,
    PlanReview,
    PlanReviewed,
    SessionState,
    TaskListItem,
    TierState,
    ToolResult,
    TurnComplete,
)
from tstd.router import TierRouter
from tstd.session import Session
from tstd.session_store import SessionStore
from tstd.shell_read_only import READ_ONLY_ENTRIES
from tstd.tools import build_decision_request, create_registry
from tstd.tools.dispatch import ToolDispatcher
from tstd.tools.registry import Tool

ROOT = Path("/work/proj")
LIST = frozenset(READ_ONLY_ENTRIES)
PLAN = "## Plan\n1. Add `greet()` to src/app.py.\n2. Test it in tests/test_app.py."


def _classify(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    planning: bool = True,
    entries: frozenset[str] = LIST,
    tool: Tool | None = None,
) -> Classification:
    found = tool if tool is not None else create_registry().require(tool_name)
    request = build_decision_request(found, arguments)
    boundary = Boundary(workspace_root=ROOT, shell_read_only=entries, plan_mode=lambda: planning)
    return DecisionClassifier(boundary).classify(request)


@pytest.fixture(autouse=True)
def _repo_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The read-only list's git probe, settled without a subprocess."""
    monkeypatch.setattr(shell_read_only, "git_ready", lambda: True)
    monkeypatch.setattr(shell_read_only, "_repo_config", {str(ROOT): shell_read_only.RepoConfig()})


# ── The classifier refuses every mutating call while planning ──────────


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("fs_write", {"path": "/work/proj/src/app.py", "content": "x"}),
        ("fs_edit", {"path": "/work/proj/src/app.py", "old_string": "a", "new_string": "b"}),
        ("fs_write", {"path": "/work/proj/.tst/memory/notes.md", "content": "x"}),
        ("shell", {"command": "rm -rf build"}),
        ("shell", {"command": "ls > listing.txt"}),
        ("shell", {"command": "ls && touch x"}),
        ("propose_steering_edit", {"path": "AGENTS.md", "content": "x", "reason": "y"}),
        ("delegate", {"task": "fix it", "kind": "worker"}),
        ("desktop_click", {"x": 1, "y": 2}),
        ("browser_click", {"selector": "#go"}),
        # The GitHub write tools declare no ``mutates``: the allowlist catches them.
        ("github_put_file", {"owner": "o", "repo": "r", "path": "a.py", "content": "x"}),
        ("github_merge", {"owner": "o", "repo": "r", "number": 1}),
        ("github_comment", {"owner": "o", "repo": "r", "number": 1, "body": "x"}),
    ],
)
def test_planning_refuses_every_call_that_could_change_something(
    tool: str, arguments: dict[str, Any]
) -> None:
    decision = _classify(tool, arguments)
    assert decision.rule is not None and decision.rule.id == "plan-mode", decision.reason
    assert decision.refused
    assert decision.decision_class is DecisionClass.C


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("fs_read", {"path": "/work/proj/src/app.py"}),
        ("fs_list", {"path": "/work/proj"}),
        ("fs_grep", {"pattern": "greet", "path": "/work/proj"}),
        ("shell", {"command": "ls -la"}),
        ("shell", {"command": "pwd"}),
        ("web_search", {"query": "pytest fixtures"}),
        ("task_list", {"tasks": [{"content": "read the code"}]}),
        ("ask_user", {"questions": []}),
        ("delegate", {"task": "find the callers"}),
        ("delegate", {"task": "find the callers", "kind": "research"}),
        ("desktop_screenshot", {}),
        ("exit_plan_mode", {"plan": PLAN}),
        ("tool_search", {"query": "issue"}),
        ("github_get_file", {"owner": "o", "repo": "r", "path": "a.py"}),
    ],
)
def test_planning_lets_reads_run(tool: str, arguments: dict[str, Any]) -> None:
    decision = _classify(tool, arguments)
    assert not decision.refused, decision.reason
    assert decision.rule is None or decision.rule.id != "plan-mode"


def test_every_builtin_is_decided_and_only_readers_run() -> None:
    """A new builtin is refused in plan mode until someone decides it reads."""
    registry = create_registry(candidate_selection=True)
    allowed = sorted(
        tool.name
        for tool in registry.list_tools()
        if tool.provenance is None
        and tool.name not in ("shell", "delegate")
        and not _classify("", {}, tool=tool).refused
    )
    assert allowed == sorted(
        {
            "ask_user",
            "browser_screenshot",
            "consult",
            "desktop_screenshot",
            "exit_plan_mode",
            "fs_grep",
            "fs_list",
            "fs_read",
            "github_get_file",
            "github_get_issue",
            "github_get_pull",
            "github_get_tree",
            "github_list_issues",
            "github_list_pulls",
            "github_list_repos",
            "github_pull_checks",
            "load_skill",
            "send_file",
            "task_list",
            "tool_search",
            "web_fetch",
            "web_search",
        }
    )


def test_a_listed_shell_command_runs_as_before_and_the_list_off_refuses_it() -> None:
    on = _classify("shell", {"command": "ls"})
    assert on.rule is not None and on.rule.id == "shell-read-only"
    off = _classify("shell", {"command": "ls"}, entries=frozenset())
    assert off.refused


def test_mcp_tools_run_only_when_the_user_marked_them_read_only() -> None:
    unmarked = Tool(name="github__get_issue", side_effect_class="ask", provenance="mcp:github")
    assert _classify("", {}, tool=unmarked).refused
    marked = replace(unmarked, read_only=True)
    decision = _classify("", {}, tool=marked)
    assert not decision.refused
    # Still judged as any MCP tool is: plan mode adds refusals, never grants.
    assert decision.rule is not None and decision.rule.id == "mcp-undeclared-fields"


def test_a_plugin_tool_is_never_read_only_in_plan_mode() -> None:
    plugin = Tool(name="lint_fix", provenance="plugin:acme", read_only=True)
    assert _classify("", {}, tool=plugin).refused


def test_outside_plan_mode_nothing_changes() -> None:
    write = _classify("fs_write", {"path": "/work/proj/src/app.py", "content": "x"}, planning=False)
    assert write.rule is not None and write.rule.id == "in-workspace-edit"
    assert not write.refused
    shell = _classify("shell", {"command": "rm -rf build"}, planning=False)
    assert shell.rule is not None and shell.rule.id == "shell-floor"


def test_the_plan_rule_comes_before_every_other_rule() -> None:
    first = RULE_TABLE[0]
    assert (first.id, first.decision_class, first.refuses) == ("plan-mode", DecisionClass.C, True)
    assert [r.id for r in RULE_TABLE if r.refuses] == ["plan-mode"]


# ── Dispatch: no policy rule, approval or skip-all runs a refused call ──


async def _dispatch_write(tmp_path: Path, *, planning: bool) -> ToolResult:
    from tstd.autonomy.worker import AmbiguousClassifier
    from tstd.tools.boundary import PathGuard
    from tstd.tools.handlers import register_builtin_handlers

    async def never_asked(*_args: Any) -> Any:
        raise AssertionError("the approval gate must not be reached")

    registry = create_registry()
    boundary = Boundary(workspace_root=tmp_path, plan_mode=lambda: planning)
    dispatcher = ToolDispatcher(
        registry,
        classifier=AmbiguousClassifier(DecisionClassifier(boundary), call_worker=never_asked),
        path_guard=PathGuard(boundary),
        # An ``auto`` rule for the very call, and skip-all on.
        policy=PolicyConfig(rules=[PolicyRule(tool="fs_write", args="**", effect="auto")]),
        approval_handler=never_asked,
        workspace=tmp_path,
    )
    dispatcher.skip_all_fn = lambda: True
    register_builtin_handlers(dispatcher)
    result = await dispatcher.dispatch(
        "tc-1", "fs_write", {"path": str(tmp_path / "out.txt"), "content": "hi"}
    )
    return result  # type: ignore[no-any-return]


async def test_a_refused_write_writes_nothing_whatever_the_policy_says(tmp_path: Path) -> None:
    result = await _dispatch_write(tmp_path, planning=True)
    assert (result.status, result.error_code) == ("error", "plan_mode")
    assert result.output.startswith("Refused: plan mode is on, so you are planning")
    assert "exit_plan_mode" in result.output
    assert not (tmp_path / "out.txt").exists()
    # The same call with plan mode off runs under that rule.
    assert (await _dispatch_write(tmp_path, planning=False)).status == "success"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hi"


def test_the_note_follows_the_persons_words() -> None:
    assert with_plan_note("fix the bug") == f"fix the bug\n\n{PLAN_NOTE}"
    parts = [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "x"}}]
    assert with_plan_note(parts) == [*parts, {"type": "text", "text": PLAN_NOTE}]


# ── A real daemon: plan mode, then the plan review ──────────────────────


def _call(tool: str, **arguments: Any) -> Script:
    return Script(kind="tool_call", tool_name=tool, tool_arguments=json.dumps(arguments))


@dataclass
class Run:
    daemon: Daemon
    provider: _Steps
    sid: str
    ws: Path

    @property
    def session(self) -> Session:
        found = self.daemon.session_registry.get(self.sid)
        assert found is not None
        return found

    @property
    def router(self) -> TierRouter:
        router = self.session.router
        assert router is not None
        return router

    def events(self, kind: type[Any]) -> list[Any]:
        return [e for e in self.session.event_log.all_events if isinstance(e, kind)]

    async def send(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        raw = await self.daemon._handle_message(json.dumps({"session_id": self.sid, **frame}), None)
        return None if raw is None else dict(json.loads(raw))

    async def plan(self, on: bool = True) -> None:
        assert await self.send({"type": "set_plan", "on": on}) is None

    async def message(self, content: str = "add a greeting") -> None:
        await self.send({"type": "user_message", "content": content})

    async def parked(self) -> PlanReview:
        await self.message()
        await eventually(lambda: bool(self.events(PlanReview)), within=20)
        review: PlanReview = self.events(PlanReview)[-1]
        return review

    async def review(self, tool_call_id: str, **body: Any) -> dict[str, Any] | None:
        return await self.send({"type": "review_plan", "tool_call_id": tool_call_id, **body})

    async def finished(self) -> list[ToolResult]:
        await eventually(lambda: bool(self.events(TurnComplete)), within=20)
        results: list[ToolResult] = self.events(ToolResult)
        return results

    def tool_reply_seen_by_model(self) -> str:
        last = self.provider.calls[-1]
        tool = [m for m in last.messages if m.role == "tool"]
        assert tool, "the model's next call carried no tool reply"
        content = tool[-1].content
        assert isinstance(content, str)
        return content


@pytest.fixture
async def make_run(tmp_path: Path) -> AsyncIterator[Any]:
    runs: list[Run] = []

    async def make(steps: list[Script]) -> Run:
        provider = _Steps([*steps, Script(kind="stream", content="Done.")])
        daemon = Daemon(data_dir=tmp_path / "data", provider=provider)
        ws = tmp_path / "ws"
        ws.mkdir()
        run = Run(daemon, provider, await _open(daemon, ws), ws)
        runs.append(run)
        return run

    yield make
    for run in runs:
        await _shutdown(run.daemon, run.sid)


async def test_a_write_while_planning_is_refused_and_the_model_is_told(make_run: Any) -> None:
    run: Run = await make_run([_call("fs_write", path="greet.py", content="x")])
    await run.plan()
    await run.message("add greet.py")
    [result] = await run.finished()
    assert (result.status, result.error_code) == ("error", "plan_mode")
    assert "you are planning" in run.tool_reply_seen_by_model()
    assert not (run.ws / "greet.py").exists()
    assert run.events(ApprovalRequest) == []
    # The message reached the model with the plan-mode note after it.
    first = run.provider.calls[0]
    user = [m for m in first.messages if m.role == "user"][-1]
    assert user.content == f"add greet.py\n\n{PLAN_NOTE}"


async def test_without_plan_mode_the_message_has_no_note(make_run: Any) -> None:
    run: Run = await make_run([])
    await run.message("hello")
    await run.finished()
    user = [m for m in run.provider.calls[0].messages if m.role == "user"][-1]
    assert user.content == "hello"


async def test_approving_ends_plan_mode_with_the_plan_pinned(make_run: Any) -> None:
    run: Run = await make_run([_call("exit_plan_mode", plan=PLAN)])
    await run.plan()
    review = await run.parked()
    assert review.plan == PLAN
    assert run.events(ApprovalRequest) == []
    await eventually(lambda: run.session.state == "awaiting_approval")
    [state] = [e for e in run.events(SessionState) if e.state == "awaiting_approval"]
    assert state.reason == "waiting for your plan review"
    # Skip-all releases parked approvals, never a plan.
    assert run.session.resolve_skippable_approvals() == 0
    assert run.router.plan_mode

    assert await run.review(review.tool_call_id, decision="approve") is None
    [result] = await run.finished()
    assert result.status == "success", result.output
    assert "approved the plan. Plan mode is off" in result.output
    assert result.output.endswith(PLAN)
    assert run.tool_reply_seen_by_model() == result.output
    assert not run.router.plan_mode
    assert run.events(TierState)[-1].plan is False
    assert run.session.pinned_plan == PLAN
    assert [p.plan for p in run.events(PlanPinned)] == [PLAN]
    [reviewed] = run.events(PlanReviewed)
    assert (reviewed.outcome, reviewed.plan, reviewed.edited) == ("approved", PLAN, False)
    assert run.session.pending_plans == {}
    assert run.events(SessionState)[-1].state == "running"


async def test_an_edited_plan_is_the_one_pinned(make_run: Any) -> None:
    run: Run = await make_run([_call("exit_plan_mode", plan=PLAN)])
    await run.plan()
    review = await run.parked()
    edited = "## Plan\n1. Add `greet()` only. No tests yet."
    assert (
        await run.review(
            review.tool_call_id, decision="approve", plan=edited, feedback="keep it small"
        )
        is None
    )
    [result] = await run.finished()
    assert "edited and approved the plan" in result.output
    assert "The user said: keep it small" in result.output
    assert result.output.endswith(edited)
    assert run.session.pinned_plan == edited
    [reviewed] = run.events(PlanReviewed)
    assert (reviewed.plan, reviewed.edited, reviewed.feedback) == (edited, True, "keep it small")


async def test_rejecting_keeps_plan_mode_on_and_returns_the_feedback(make_run: Any) -> None:
    run: Run = await make_run([_call("exit_plan_mode", plan=PLAN)])
    await run.plan()
    review = await run.parked()
    assert (
        await run.review(review.tool_call_id, decision="reject", feedback="use the logger instead")
        is None
    )
    [result] = await run.finished()
    assert result.status == "success"
    assert "rejected the plan. Plan mode is still on" in result.output
    assert "The user said: use the logger instead" in result.output
    assert run.router.plan_mode
    assert run.session.pinned_plan == ""
    assert run.events(PlanPinned) == []
    assert run.events(PlanReviewed)[0].outcome == "rejected"


async def test_review_refusals(make_run: Any) -> None:
    run: Run = await make_run([_call("exit_plan_mode", plan=PLAN)])
    await run.plan()
    review = await run.parked()
    empty = await run.review(review.tool_call_id, decision="approve", plan="   ")
    assert empty is not None and empty["code"] == "bad_plan"
    assert empty["in_reply_to"] == "review_plan"
    assert set(run.session.pending_plans) == {review.tool_call_id}
    assert await run.review(review.tool_call_id, decision="approve") is None
    await run.finished()
    again = await run.review(review.tool_call_id, decision="approve")
    assert again is not None and again["code"] == "no_pending_plan"


async def test_stop_ends_the_review_and_plan_mode_stays_on(make_run: Any) -> None:
    run: Run = await make_run([_call("exit_plan_mode", plan=PLAN)])
    await run.plan()
    await run.parked()
    await run.send({"type": "stop_turn"})
    await eventually(lambda: bool(run.events(PlanReviewed)), within=20)
    assert run.events(PlanReviewed)[0].outcome == "stopped"
    assert run.router.plan_mode
    assert run.session.pending_plans == {}


async def test_exit_plan_mode_outside_plan_mode_is_refused(make_run: Any) -> None:
    run: Run = await make_run([_call("exit_plan_mode", plan=PLAN)])
    await run.message()
    [result] = await run.finished()
    assert (result.status, result.error_code) == ("error", "plan_mode_off")
    assert run.events(PlanReview) == []


async def test_delegate_worker_is_refused_while_planning(make_run: Any) -> None:
    run: Run = await make_run([_call("delegate", task="write greet.py", kind="worker")])
    await run.plan()
    await run.message()
    [result] = await run.finished()
    assert (result.status, result.error_code) == ("error", "plan_mode")
    assert "kind research" in result.output


# ── The pinned plan survives compaction and restart ─────────────────────


def test_compaction_carries_the_plan_ahead_of_the_task_list(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    assert carry_for(session) == ""
    session.pinned_plan = PLAN
    session.task_list = [TaskListItem(content="read the code", status="done")]
    carried = carry_for(session)
    assert carried.startswith("The plan the user approved (pinned")
    assert carried.index(PLAN) < carried.index("Your task list")


async def test_a_restart_reads_the_pinned_plan_back(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    restore_pinned_plan(
        session,
        [
            PlanPinned(session_id=session.id, plan="old", seq=1),
            PlanPinned(session_id=session.id, plan=PLAN, seq=2),
        ],
    )
    assert session.pinned_plan == PLAN
    # The log lost it: it goes back in, once.
    await keep_pinned_plan_visible(session)
    await keep_pinned_plan_visible(session)
    pinned = [e for e in session.event_log.all_events if isinstance(e, PlanPinned)]
    assert [p.plan for p in pinned] == [PLAN]


# ── A restart does not end plan mode ────────────────────────────────────


async def test_plan_mode_outlives_a_restart_and_writes_stay_refused(tmp_path: Path) -> None:
    """A deploy's stop and launch must not quietly turn writes back on.

    Plan mode is read-only, so only approving a plan or turning it off ends
    it: the revived session is still planning, says so on ``tier_state``,
    and a write the next turn asks for is refused.
    """
    data_dir = tmp_path / "data"
    ws = tmp_path / "ws"
    ws.mkdir()
    first = Daemon(data_dir=data_dir, provider=_Steps([]))
    sid = await _open(first, ws)
    before = Run(first, _Steps([]), sid, ws)
    await before.plan()
    await _shutdown(first, sid)

    provider = _Steps(
        [_call("fs_write", path="greet.py", content="x"), Script(kind="stream", content="Done.")]
    )
    second = Daemon(data_dir=data_dir, provider=provider)
    await second._restore_sessions()
    run = Run(second, provider, sid, ws)
    try:
        assert run.router.plan_mode
        assert run.events(TierState)[-1].plan is True
        await run.message("add greet.py")
        [result] = await run.finished()
        assert (result.status, result.error_code) == ("error", "plan_mode")
        assert not (ws / "greet.py").exists()
        user = [m for m in provider.calls[0].messages if m.role == "user"][-1]
        assert user.content == f"add greet.py\n\n{PLAN_NOTE}"
    finally:
        await _shutdown(second, sid)


async def test_the_row_follows_every_plan_change_and_approving_ends_it(
    make_run: Any, tmp_path: Path
) -> None:
    run: Run = await make_run([_call("exit_plan_mode", plan=PLAN)])

    def on_disk() -> bool | None:
        record = SessionStore(tmp_path / "data").get(run.sid)
        assert record is not None
        return record.plan_mode

    assert on_disk() is False
    await run.plan()
    assert on_disk() is True
    await run.plan(False)
    assert on_disk() is False
    await run.plan()
    review = await run.parked()
    assert on_disk() is True
    assert await run.review(review.tool_call_id, decision="approve") is None
    await run.finished()
    assert on_disk() is False


def test_a_row_from_before_the_field_reads_plan_mode_from_the_log(tmp_path: Path) -> None:
    (tmp_path / "sessions.json").write_text(
        json.dumps([{"session_id": "s", "workspace_path": "/w", "state": "idle"}]),
        encoding="utf-8",
    )
    record = SessionStore(tmp_path).get("s")
    assert record is not None
    assert record.plan_mode is None
    on = TierState(session_id="s", tier="brain", plan=True, seq=1)
    off = TierState(session_id="s", tier="brain", plan=False, seq=2)
    assert restored_plan_mode(None, [off, on]) is True
    assert restored_plan_mode(None, [on, off]) is False
    assert restored_plan_mode(None, []) is False
    # A row that has the field wins over the log.
    assert restored_plan_mode(False, [on]) is False
    assert restored_plan_mode(True, []) is True


# ── Who may review, and who hears about it ──────────────────────────────


def test_a_phone_or_paired_computer_may_review_and_it_is_never_queued() -> None:
    assert "review_plan" in peer_policy.MACHINE_PEER_VERBS
    assert "review_plan" in peer_policy.FORWARDED_VERBS
    assert "review_plan" in peer_policy.APPROVAL_VERBS


def test_every_channel_says_a_plan_is_waiting() -> None:
    event = PlanReview(session_id="s", tool_call_id="t", plan=PLAN, seq=1)
    for channel in (ntfy, slack, discord, telegram):
        assert channel.message_for(event) == "Plan ready for review: Plan"


async def test_a_manual_compaction_carries_the_pinned_plan(tmp_path: Path) -> None:
    from tests.test_compaction import pad, tiny_config
    from tstd.compaction import ContextMeter
    from tstd.context.tokens import HeuristicTokenCounter
    from tstd.provider import ChatMessage, content_as_text

    session = Session(str(tmp_path))
    session.conversation.append(ChatMessage(role="system", content="You are helpful."))
    for n in range(3):
        session.conversation.append(ChatMessage(role="user", content=pad(f"question {n}", 400)))
        session.conversation.append(ChatMessage(role="assistant", content=pad(f"answer {n}", 400)))
    counter = HeuristicTokenCounter()
    session.context_meter = ContextMeter("brain", tiny_config().tier("brain"), counter, None)
    session.pinned_plan = PLAN

    assert await session.compact_now() is None
    assert content_as_text(session.conversation[1].content).endswith(PLAN)


# ── MCP tools are marked read-only by the user's config only ────────────


def test_only_listed_mcp_tools_are_read_only(tmp_path: Path) -> None:
    from tstd.config import McpConfig, McpServerConfig
    from tstd.mcp.jsonrpc import McpRemoteTool
    from tstd.mcp.loader import McpSupervisor, _LiveServer
    from tstd.tools.registry import ToolRegistry

    spec = McpServerConfig(transport="stdio", command=["srv"], read_only_tools=["get_issue"])
    supervisor = McpSupervisor(McpConfig(servers={"github": spec}))
    remote = [
        McpRemoteTool(name, "", {"type": "object", "properties": {}})
        for name in ("get_issue", "create_issue")
    ]
    supervisor._live = {"github": _LiveServer("github", object(), remote)}  # type: ignore[arg-type]
    registry = ToolRegistry()
    dispatcher = ToolDispatcher(registry)
    supervisor.attach(registry, dispatcher)
    marked = {t.name: t.read_only for t in registry.list_tools()}
    assert marked == {"github__get_issue": True, "github__create_issue": False}
    assert not _classify("", {}, tool=registry.require("github__get_issue")).refused
    assert _classify("", {}, tool=registry.require("github__create_issue")).refused


def test_the_list_survives_a_settings_rewrite(tmp_path: Path) -> None:
    from tstd.config import McpServerConfig, load_config
    from tstd.config_write import save_mcp_server

    path = tmp_path / "config.yaml"
    spec = McpServerConfig(transport="stdio", command=["srv"], read_only_tools=["get_issue"])
    save_mcp_server("github", spec, path)
    assert load_config(path).mcp.servers["github"].read_only_tools == ["get_issue"]
    with pytest.raises(ValueError, match="empty tool name"):
        McpServerConfig(transport="stdio", command=["srv"], read_only_tools=[" "])


# ── tst run: the plan waits for the window ──────────────────────────────


async def test_tst_run_exits_approval_needed_on_a_plan() -> None:
    from tests.test_tst_approvals import _Ws
    from tstd import cli, cli_turn

    event = {"type": "plan_review", "session_id": "s1", "tool_call_id": "t", "plan": PLAN}
    for output in ("text", "json", "ndjson"):
        ws = _Ws([event])
        outcome = await cli_turn.stream_turn(ws, "s1", output=output)  # type: ignore[arg-type]
        assert outcome.exit_code == cli_turn.EXIT_APPROVAL_NEEDED
        assert ws.sent == []
    assert cli._visible_event_line(event) == cli.PLAN_WAITING
    assert "plan_mode" in cli_turn.REFUSAL_CODES
