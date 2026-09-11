"""Tests for the one-level worker subagent (TD-4602)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.test_dispatch import (
    attach_auto_approver,
    make_classifier,
    make_config,
    start_loop,
    wait_for_turn,
)
from tstd.autonomy import Boundary, DecisionClass, DecisionClassifier
from tstd.boundary_config import CapsSection
from tstd.context import PromptAssembler
from tstd.cost import CostTracker
from tstd.mock import MockProvider, Script
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.router import TierRouter
from tstd.session import Session, SessionRegistry
from tstd.tools import (
    UnclassifiedToolCall,
    create_registry,
    register_builtin_handlers,
)
from tstd.tools.boundary import PathGuard
from tstd.tools.delegate import (
    CHILD_MAX_ITERATIONS,
    SUMMARY_CHAR_CAP,
    DelegateRuntime,
    cap_worker_summary,
    handle_delegate,
    run_worker_child,
    worker_child_registry,
)
from tstd.tools.dispatch import ToolDispatcher, build_decision_request
from tstd.tools.results import HandlerRefusal


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.md").write_text("hello from notes\n", encoding="utf-8")
    (ws / "AGENTS.md").write_text("root rules\n", encoding="utf-8")
    return ws


def _parent_dispatcher(ws: Path) -> ToolDispatcher:
    registry = create_registry()
    dispatcher = attach_auto_approver(
        ToolDispatcher(
            registry,
            classifier=make_classifier(str(ws)),
            path_guard=PathGuard(Boundary(workspace_root=ws)),
            workspace=ws,
        )
    )
    register_builtin_handlers(dispatcher)
    return dispatcher


def _bind_runtime(
    session: Session,
    dispatcher: ToolDispatcher,
    mock: MockProvider,
    *,
    iterations: int = 0,
) -> DelegateRuntime:
    config = make_config()
    session.cost_tracker = CostTracker(config)
    runtime = DelegateRuntime(
        provider_factory=_zero_arg_factory(mock),
        config=config,
        dispatcher=dispatcher,
        assembler=PromptAssembler(session.workspace_path),
        session_start=time.time(),
        iterations=iterations,
    )
    session.delegate_runtime = runtime
    return runtime


def _zero_arg_factory(mock: MockProvider):
    async def _factory() -> MockProvider:
        return mock

    return _factory


# ── Tool declaration ────────────────────────────────────────────────────


def test_delegate_is_registered_as_ask_with_no_path_or_host_fields() -> None:
    registry = create_registry()
    tool = registry.require("delegate")
    assert tool.side_effect_class == "ask"
    assert tool.path_fields == ()
    assert tool.host_fields == ()
    assert tool.host_resolver is None


def test_child_registry_has_fs_and_shell_but_not_delegate() -> None:
    child = worker_child_registry(create_registry())
    assert child.get("delegate") is None
    assert child.get("fs_read") is not None
    assert child.get("fs_write") is not None
    assert child.get("shell") is not None


def test_delegate_classifies_as_b_ask_floor(tmp_path: Path) -> None:
    tool = create_registry().require("delegate")
    request = build_decision_request(tool, {"task": "read notes.md"})
    decision = DecisionClassifier(Boundary(workspace_root=tmp_path)).classify(request)
    assert decision.decision_class is DecisionClass.B
    assert decision.rule is not None
    assert decision.rule.id == "side-effect-ask-floor"


async def test_dispatch_without_classifier_raises() -> None:
    registry = create_registry()
    dispatcher = ToolDispatcher(registry)
    register_builtin_handlers(dispatcher)
    with pytest.raises(UnclassifiedToolCall):
        await dispatcher.dispatch("c1", "delegate", {"task": "x"})


# ── Child runner ────────────────────────────────────────────────────────


async def test_worker_child_reads_then_summarizes(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-worker": [
                Script(
                    kind="tool_call",
                    tool_name="fs_read",
                    tool_arguments='{"path": "notes.md"}',
                ),
                Script(kind="text", content="notes.md says hello from notes."),
            ]
        }
    )
    _bind_runtime(parent, dispatcher, mock)

    summary = await run_worker_child(parent=parent, task="Read notes.md and summarize")

    assert "hello from notes" in summary
    worker_calls = [c for c in mock.calls if c.model == "test-worker"]
    assert worker_calls
    for call in worker_calls:
        names = [t.function.name for t in (call.tools or []) if t.function is not None]
        assert "delegate" not in names
        assert "fs_read" in names
    tracker = parent.cost_tracker
    assert tracker is not None
    worker_records = [c for c in tracker.calls if c.source == "worker"]
    assert worker_records
    assert all(c.tier == "worker" for c in worker_records)


async def test_nested_delegate_is_refused(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    child = Session(str(ws))
    child.delegate_depth = 1
    dispatcher = _parent_dispatcher(ws)
    result = await dispatcher.dispatch(
        "c1",
        "delegate",
        {"task": "should not run"},
        session=child,
    )
    assert result.status == "error"
    assert result.error_code == "delegate_nested"
    assert "grandchildren" in result.output


async def test_handle_delegate_refuses_at_depth(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    session.delegate_depth = 1
    with pytest.raises(HandlerRefusal, match="grandchildren"):
        await handle_delegate(session, task="nope")


async def test_parent_spend_cap_stops_child(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
    parent.boundary_config.caps = CapsSection(spend_usd=0.0)
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(default=Script(kind="text", content="should not run"))
    _bind_runtime(parent, dispatcher, mock)

    summary = await run_worker_child(parent=parent, task="anything")

    assert "spend cap" in summary.lower()
    assert mock.calls == []
    tracker = parent.cost_tracker
    assert tracker is not None
    assert tracker.calls == []


async def test_child_write_uses_parent_path_guard(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-worker": [
                Script(
                    kind="tool_call",
                    tool_name="fs_write",
                    tool_arguments='{"path": "AGENTS.md", "content": "pwned"}',
                ),
                Script(kind="text", content="Could not write the steering file."),
            ]
        }
    )
    _bind_runtime(parent, dispatcher, mock)

    summary = await run_worker_child(parent=parent, task="Overwrite AGENTS.md")

    assert (ws / "AGENTS.md").read_text(encoding="utf-8") == "root rules\n"
    assert "steering" in summary.lower() or "Could not write" in summary


async def test_session_registry_does_not_grow(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    registry = SessionRegistry()
    parent = await registry.create(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(default=Script(kind="text", content="done"))
    _bind_runtime(parent, dispatcher, mock)

    await run_worker_child(parent=parent, task="Say done")

    assert registry.count == 1
    listed = await registry.list_sessions()
    assert listed == [parent]


async def test_child_does_not_start_nested_autonomy(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
    parent.autonomy = True
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(default=Script(kind="text", content="ok"))
    _bind_runtime(parent, dispatcher, mock)

    summary = await run_worker_child(parent=parent, task="ok")

    assert summary == "ok"
    assert parent.autonomy is True
    assert parent.dod_poller is None


def test_summary_is_capped() -> None:
    long = "x" * (SUMMARY_CHAR_CAP + 50)
    capped = cap_worker_summary(long)
    assert len(capped) <= SUMMARY_CHAR_CAP
    assert "truncated" in capped
    assert CHILD_MAX_ITERATIONS == 8


# ── Parent loop ─────────────────────────────────────────────────────────


async def test_parent_loop_delegate_returns_capped_summary(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    session = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(
                    kind="tool_call",
                    tool_name="delegate",
                    tool_arguments='{"task": "Read notes.md and summarize"}',
                ),
                Script(kind="stream", content="The worker finished."),
            ],
            "test-worker": [
                Script(
                    kind="tool_call",
                    tool_name="fs_read",
                    tool_arguments='{"path": "notes.md"}',
                ),
                Script(kind="text", content="notes.md says hello from notes."),
            ],
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
        await session.add_user_message("Delegate the read")
        await wait_for_turn(session, 1)
    finally:
        await runner.cancel()

    tool_calls = [e for e in session.event_log.all_events if isinstance(e, ToolCallEvent)]
    assert [e.name for e in tool_calls] == ["delegate"]
    results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
    assert results
    assert "hello from notes" in results[0].output
    tracker = session.cost_tracker
    assert tracker is not None
    assert any(c.source == "worker" and c.tier == "worker" for c in tracker.calls)
    worker_calls = [c for c in mock.calls if c.model == "test-worker"]
    for call in worker_calls:
        names = [t.function.name for t in (call.tools or []) if t.function is not None]
        assert "delegate" not in names
