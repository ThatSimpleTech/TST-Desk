"""TD-5122: the native loop keeps a task list the user can see.

``task_list`` goes through the real dispatcher (classifier, policy) and
replaces the session's list. Each change logs one ``task_list`` event with
the whole list, which the Plan pane and the phone read. A compaction, by
the budget or on request, carries the list into its summary, so the plan
outlives the turns that made it. A restart rebuilds it from the log.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.test_compaction import pad, tiny_config
from tests.test_dispatch import make_classifier
from tests.test_loop import mock_factory, wait_for_turn
from tests.test_queued_restart import _restored, _stop
from tstd.autonomy import Boundary, DecisionClass, DecisionClassifier
from tstd.autonomy.classifier import RULE_TABLE, DecisionRequest
from tstd.compaction import ContextMeter, compact_on_request, maybe_compact
from tstd.context import PromptAssembler
from tstd.context.tokens import HeuristicTokenCounter
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.policy import ApprovalOutcome, PolicyConfig, PolicyRule
from tstd.protocol import ContextCompacted, TaskList, TaskListItem, UserTurn
from tstd.provider import ChatMessage, content_as_text, wire_messages
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.task_list import carry_text, keep_task_list_visible, restore_task_list
from tstd.tools import ToolDispatcher
from tstd.tools.delegate import RESEARCH_CHILD_TOOLS, WORKER_CHILD_TOOLS
from tstd.tools.handlers import register_builtin_handlers
from tstd.tools.registry import ToolRegistry, create_registry
from tstd.tools.task_list import TASK_LIST_TOOL, register_task_list_tool, task_list

COUNTER = HeuristicTokenCounter()
CARRY = "[Kept across compaction]"
PLAN = [
    {"content": "Read the failing test", "status": "done"},
    {"content": "Fix the date parser", "status": "in_progress"},
    {"content": "Run the suite", "status": "pending"},
]


@dataclass
class Env:
    ws: Path
    session: Session
    dispatcher: ToolDispatcher
    approvals: list[tuple[Any, ...]] = field(default_factory=list)

    def lists(self) -> list[TaskList]:
        return [e for e in self.session.event_log.all_events if isinstance(e, TaskList)]

    async def call(self, arguments: dict[str, Any]) -> Any:
        return await self.dispatcher.dispatch("call_1", "task_list", arguments, self.session)


def _env(tmp_path: Path, registry: ToolRegistry | None = None) -> Env:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("Test workspace steering.\n")
    session = Session(str(ws))
    dispatcher = ToolDispatcher(
        registry or create_registry(), classifier=make_classifier(str(ws)), workspace=ws
    )
    dispatcher.policy = PolicyConfig()
    env = Env(ws, session, dispatcher)

    async def _approve(*args: Any) -> ApprovalOutcome:
        env.approvals.append(args)
        return ApprovalOutcome(True)

    dispatcher.approval_handler = _approve
    if registry is None:
        register_builtin_handlers(dispatcher)
    else:
        dispatcher.register_handler("task_list", task_list)
    return env


def _items(plan: list[dict[str, str]]) -> list[TaskListItem]:
    return [TaskListItem.model_validate(task) for task in plan]


# ── The tool ─────────────────────────────────────────────────────────────


def test_the_native_loop_offers_task_list() -> None:
    registry = create_registry()
    tool = registry.require("task_list")
    assert tool is TASK_LIST_TOOL
    status = tool.parameters["properties"]["tasks"]["items"]["properties"]["status"]
    assert status["enum"] == ["pending", "in_progress", "done"]
    assert "task_list" in {d.function.name for d in registry.to_provider_definitions()}
    # A delegated child has its own short list; the plan is the parent's.
    assert "task_list" not in WORKER_CHILD_TOOLS
    assert "task_list" not in RESEARCH_CHILD_TOOLS


async def test_a_call_creates_the_list_with_no_card(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = await env.call({"tasks": PLAN})

    assert result.status == "success", result.output
    assert result.decision_class is DecisionClass.A
    assert env.approvals == []
    assert env.session.task_list == _items(PLAN)
    [event] = env.lists()
    assert event.session_id == env.session.id
    assert event.items == _items(PLAN)
    assert result.output.splitlines() == [
        "Task list saved: 1 of 3 done.",
        "1. [done] Read the failing test",
        "2. [in progress] Fix the date parser",
        "3. [pending] Run the suite",
    ]


async def test_each_call_replaces_the_whole_list(tmp_path: Path) -> None:
    env = _env(tmp_path)
    await env.call({"tasks": PLAN})
    updated = [
        {"content": "Read the failing test", "status": "done"},
        {"content": "Fix the date parser", "status": "done"},
        {"content": "Run the suite", "status": "in_progress"},
        {"content": "Update the changelog", "status": "pending"},
    ]
    result = await env.call({"tasks": updated})

    assert env.session.task_list == _items(updated)
    assert [e.items for e in env.lists()] == [_items(PLAN), _items(updated)]
    assert result.output.startswith("Task list saved: 2 of 4 done.")

    finished = [{**task, "status": "done"} for task in updated]
    assert (await env.call({"tasks": finished})).output.startswith("Task list saved: all 4 done.")

    cleared = await env.call({"tasks": []})
    assert cleared.output == "Task list cleared."
    assert env.session.task_list == []
    assert env.lists()[-1].items == []


async def test_one_item_is_one_line(tmp_path: Path) -> None:
    env = _env(tmp_path)
    await env.call({"tasks": [{"content": "  Fix\nthe   parser ", "status": "pending"}]})
    assert env.session.task_list == [TaskListItem(content="Fix the parser", status="pending")]


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"tasks": [{"content": "Fix it", "status": "completed"}]}, "invalid_arguments"),
        ({"tasks": [{"content": "Fix it"}]}, "invalid_arguments"),
        ({"tasks": [{"content": "", "status": "pending"}]}, "invalid_arguments"),
        ({"tasks": [{"content": "x" * 201, "status": "pending"}]}, "invalid_arguments"),
        ({"tasks": [{"content": "x", "status": "pending"}] * 31}, "invalid_arguments"),
        ({"tasks": [{"content": "   ", "status": "pending"}]}, "task_list_invalid"),
        ({}, "invalid_arguments"),
    ],
)
async def test_a_bad_list_saves_nothing(
    tmp_path: Path, arguments: dict[str, Any], code: str
) -> None:
    env = _env(tmp_path)
    await env.call({"tasks": PLAN})
    result = await env.call(arguments)

    assert result.status == "error"
    assert result.error_code == code
    assert env.session.task_list == _items(PLAN)
    assert len(env.lists()) == 1


async def test_a_policy_rule_still_wins(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env.dispatcher.policy = PolicyConfig(rules=[PolicyRule(tool="task_list", effect="ask")])
    assert (await env.call({"tasks": PLAN})).status == "success"
    assert len(env.approvals) == 1

    env.dispatcher.policy = PolicyConfig(rules=[PolicyRule(tool="task_list", effect="never")])
    refused = await env.call({"tasks": []})
    assert refused.status == "error"
    assert env.session.task_list == _items(PLAN)


async def test_without_a_session_the_handler_refuses() -> None:
    from tstd.tools.results import HandlerRefusal

    with pytest.raises(HandlerRefusal) as refused:
        await task_list(session=object(), tasks=PLAN)
    assert refused.value.code == "task_list_unavailable"


# ── The classifier ───────────────────────────────────────────────────────


def _classify(request: DecisionRequest) -> Any:
    return DecisionClassifier(Boundary(workspace_root=Path("/work/proj"))).classify(request)


def test_the_builtin_shape_is_class_a() -> None:
    decision = _classify(DecisionRequest(tool_name="task_list"))
    assert decision.decision_class is DecisionClass.A
    assert decision.rule is not None and decision.rule.id == "task-list"


def test_only_the_builtin_shape_matches() -> None:
    for request in (
        DecisionRequest(tool_name="task_list", provenance="mcp:srv"),
        DecisionRequest(tool_name="task_list", provenance="plugin:dist"),
        DecisionRequest(tool_name="task_list", side_effect_class="ask"),
        DecisionRequest(tool_name="task_list", reads=(Path("/work/proj/a"),)),
        DecisionRequest(tool_name="task_list", writes=(Path("/work/proj/a"),), is_mutation=True),
        DecisionRequest(tool_name="task_list", hosts=("example.com",)),
        DecisionRequest(tool_name="todo_write"),
    ):
        decision = _classify(request)
        assert decision.rule is None or decision.rule.id != "task-list", request


def test_order_pinned() -> None:
    ids = [rule.id for rule in RULE_TABLE]
    at = ids.index("task-list")
    assert ids.index("cap-exceeded") < at
    assert ids.index("side-effect-never") < at
    assert ids.index("mcp-undeclared-fields") < at
    assert at < ids.index("side-effect-ask-floor")


# ── Compaction carries it ────────────────────────────────────────────────


def _conversation(turns: int) -> list[ChatMessage]:
    messages = [ChatMessage(role="system", content="You are helpful.")]
    for n in range(turns):
        messages.append(ChatMessage(role="user", content=pad(f"question {n}", 400)))
        messages.append(ChatMessage(role="assistant", content=pad(f"answer {n}", 400)))
    return messages


def test_the_summary_carries_the_list_whole() -> None:
    carry = carry_text(_items(PLAN))
    compacted, stats = maybe_compact(
        _conversation(4), tiny_config().tier("brain"), COUNTER, carry=carry
    )

    assert stats is not None
    summary = content_as_text(compacted[1].content)
    assert summary.startswith("[Earlier conversation compacted]")
    assert summary.endswith(f"{CARRY}\n{carry}")
    assert "2. [in progress] Fix the date parser" in summary


def test_no_list_carries_nothing() -> None:
    compacted, stats = maybe_compact(_conversation(4), tiny_config().tier("brain"), COUNTER)
    assert stats is not None
    assert CARRY not in content_as_text(compacted[1].content)


def test_a_later_compaction_carries_only_the_newest_list() -> None:
    first = compact_on_request(_conversation(3), COUNTER, carry=carry_text(_items(PLAN)))
    assert first is not None
    messages = first[0]
    messages += _conversation(2)[1:]
    newer = [{**task, "status": "done"} for task in PLAN]
    second = compact_on_request(messages, COUNTER, carry=carry_text(_items(newer)))
    assert second is not None

    summary = content_as_text(second[0][1].content)
    assert summary.count(CARRY) == 1
    assert summary.endswith(carry_text(_items(newer)))
    assert "[in progress]" not in summary


def test_a_from_here_summary_carries_it_too() -> None:
    result = compact_on_request(
        _conversation(3), COUNTER, from_position=3, carry=carry_text(_items(PLAN))
    )
    assert result is not None
    summary = content_as_text(result[0][-1].content)
    assert summary.endswith(carry_text(_items(PLAN)))


async def test_compact_on_request_carries_the_sessions_list(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    session.conversation.extend(_conversation(3))
    session.context_meter = ContextMeter("brain", tiny_config().tier("brain"), COUNTER, None)
    session.task_list = _items(PLAN)

    assert await session.compact_now() is None
    assert content_as_text(session.conversation[1].content).endswith(carry_text(_items(PLAN)))


async def test_the_loop_reinjects_the_list_after_compaction(tmp_path: Path) -> None:
    """The model makes a list, then turns pile up until the oldest compacts:
    the next request has the list although the call that made it is gone."""
    registry = ToolRegistry()
    register_task_list_tool(registry)
    env = _env(tmp_path, registry)
    reply = Script(kind="stream", content=pad("reply", 800))
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(
                    kind="tool_call",
                    tool_name="task_list",
                    tool_arguments=json.dumps({"tasks": PLAN}),
                ),
                reply,
                reply,
                reply,
            ]
        }
    )
    runner = SessionRunner(
        env.session,
        loop_factory=lambda s: agent_loop(
            s,
            TierRouter(),
            mock_factory(mock),
            tiny_config(context_window=1200),
            prompt_assembler=PromptAssembler(env.ws, home_dir=tmp_path / "home"),
            tool_registry=registry,
            tool_dispatcher=env.dispatcher,
        ),
    )
    await runner.start()
    try:
        for n in range(1, 4):
            await env.session.add_user_message(pad(f"turn{n}", 800))
            await wait_for_turn(env.session, n)
    finally:
        await runner.cancel()

    assert env.approvals == []
    compacted = [e for e in env.session.event_log.all_events if isinstance(e, ContextCompacted)]
    assert any(e.trigger == "auto" for e in compacted)
    sent = wire_messages(mock.calls[-1].messages)
    assert not any(m.tool_calls for m in sent), "the task_list call itself was compacted away"
    assert sent[0].role == "system"
    assert content_as_text(sent[0].content).endswith(carry_text(_items(PLAN)))


# ── Restart and attach ───────────────────────────────────────────────────


def test_restore_takes_the_newest_list() -> None:
    session = Session("/tmp/ws")
    older = TaskList(session_id=session.id, items=_items(PLAN), seq=1)
    newer = TaskList(session_id=session.id, items=_items(PLAN)[:1], seq=5)
    restore_task_list(
        session, [older, UserTurn(session_id=session.id, turn_id="t1", content="go", seq=3), newer]
    )
    assert session.task_list == _items(PLAN)[:1]

    cleared = Session("/tmp/ws")
    restore_task_list(cleared, [older, TaskList(session_id=cleared.id, items=[], seq=2)])
    assert cleared.task_list == []


async def test_a_revived_session_keeps_its_list(tmp_path: Path) -> None:
    from tests.test_queued_restart import SID

    events = [TaskList(session_id=SID, items=_items(PLAN), seq=1)]
    daemon = await _restored(tmp_path, [], events)
    try:
        session = daemon.session_registry.get(SID)
        assert session is not None
        assert session.task_list == _items(PLAN)
    finally:
        await _stop(daemon)


async def test_a_list_the_log_window_lost_is_logged_again() -> None:
    session = Session("/tmp/ws")
    await keep_task_list_visible(session)
    assert session.event_log.all_events == []  # no list, nothing to show

    session.task_list = _items(PLAN)
    await keep_task_list_visible(session)
    [event] = session.event_log.all_events
    assert isinstance(event, TaskList) and event.items == _items(PLAN)

    await keep_task_list_visible(session)
    assert len(session.event_log.all_events) == 1  # still in the window
