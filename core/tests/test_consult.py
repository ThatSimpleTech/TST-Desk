"""The ``consult`` tool (TD-5119): the acting tier asks a stronger one.

A worker turn, or a delegate child, sends the brain (or the validator) a
short question and the context it picks, and gets the answer back as the
tool result. Config offers the tool per tier (the worker by default) and
caps it per turn; each consult is a call on the tier it asks and shows on
the cost meter.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
import yaml

from tests.test_delegate import _bind_runtime, _parent_dispatcher, _workspace
from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tstd.autonomy.classifier import Boundary, DecisionClass, DecisionClassifier
from tstd.config import ConsultConfig, ModelConfig, default_config_yaml, load_config
from tstd.mock import MockProvider, Script
from tstd.policy import PolicyConfig, PolicyRule, resolve
from tstd.protocol import CostUpdate
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.provider import ChatCompletionRequest
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import create_registry
from tstd.tools.consult import ANSWER_TOKEN_CAP, CONSULT_TOOL, handle_consult
from tstd.tools.delegate import run_worker_child, worker_child_registry
from tstd.tools.dispatch import build_decision_request
from tstd.tools.registry import Tool
from tstd.tools.results import HandlerRefusal

QUESTION = "Should the cache key include the locale?"
CONTEXT = "def key(user, locale): return user.id  # locale ignored today"


def _config(**consult: object) -> ModelConfig:
    config = make_config()
    if consult:
        config.consult = ConsultConfig.model_validate(consult)
    return config


def _offered(mock: MockProvider, model: str) -> list[set[str]]:
    return [
        {t.function.name for t in (call.tools or []) if t.function is not None}
        for call in mock.calls
        if call.model == model and call.tools
    ]


def _consult_calls(mock: MockProvider, model: str) -> list[ChatCompletionRequest]:
    """Calls to *model* that carried no tools: the consult requests."""
    return [c for c in mock.calls if c.model == model and not c.tools]


def _consult_args(**extra: str) -> str:
    return json.dumps({"question": QUESTION, "context": CONTEXT, **extra})


def _worker_session(tmp_path: Path, mock: MockProvider, config: ModelConfig) -> Session:
    ws = _workspace(tmp_path)
    session = Session(str(ws))
    runtime = _bind_runtime(session, _parent_dispatcher(ws), mock)
    runtime.config = config
    session.acting_tier = "worker"
    return session


#: An adviser answer that takes 10 s to stream: Stop must not wait for it.
SLOW_ANSWER = Script(kind="stream", content="slow " * 40, chunk_delay=0.25)


def _track_stream_close(mock: MockProvider) -> list[str]:
    """Record the model of every stream the caller closed before its end."""
    closed: list[str] = []
    real = mock.chat_completion_stream

    def tracked(request: ChatCompletionRequest):  # type: ignore[no-untyped-def]
        async def stream():  # type: ignore[no-untyped-def]
            finished = False
            try:
                async for item in real(request):
                    yield item
                finished = True
            finally:
                if not finished:
                    closed.append(request.model)

        return stream()

    mock.chat_completion_stream = tracked  # type: ignore[method-assign]
    return closed


async def _turn_running(session: Session) -> None:
    """Put *session* mid-turn, so Stop is not ignored as idle."""
    await session.set_state("running")
    await session.add_user_message("Fix the cache key")


# ── Config ──────────────────────────────────────────────────────────────


def test_default_offers_consult_to_the_worker_only() -> None:
    cfg = ConsultConfig()
    assert cfg.tiers == ["worker"]
    assert cfg.max_per_turn == 2
    assert cfg.allows("worker")
    assert not cfg.allows("brain")
    assert not cfg.allows("validator")


def test_shipped_config_lists_the_worker_only() -> None:
    shipped = yaml.safe_load(default_config_yaml())
    assert shipped["consult"] == {"tiers": ["worker"], "max_per_turn": 2}


def test_older_user_copy_gets_the_shipped_block(tmp_path: Path) -> None:
    data = yaml.safe_load(default_config_yaml())
    data.pop("consult")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_config(path).consult.tiers == ["worker"]


def test_tiers_can_be_emptied_and_reject_unknown_names() -> None:
    assert not ConsultConfig(tiers=[]).allows("worker")
    assert ConsultConfig.model_validate({"tiers": ["worker", "worker"]}).tiers == ["worker"]
    with pytest.raises(ValueError):
        ConsultConfig.model_validate({"tiers": ["planner"]})
    with pytest.raises(ValueError):
        ConsultConfig.model_validate({"max_per_turn": 0})


# ── Handler ─────────────────────────────────────────────────────────────


async def test_worker_asks_the_brain_and_gets_the_answer(tmp_path: Path) -> None:
    mock = MockProvider(
        sequences={"test-brain": [Script(kind="text", content="Yes: add the locale.")]}
    )
    session = _worker_session(tmp_path, mock, _config())

    out = await handle_consult(session=session, question=QUESTION, context=CONTEXT)

    assert "Yes: add the locale." in out
    assert "<consult_answer>" in out
    assert "Consults left this turn: 1." in out
    [call] = mock.calls
    assert call.model == "test-brain"
    assert call.tools is None
    # The adviser gets its instructions and the question with the chosen
    # context, and nothing else.
    assert [m.role for m in call.messages] == ["system", "user"]
    user = call.messages[1].content
    assert isinstance(user, str) and QUESTION in user and CONTEXT in user


async def test_to_validator_asks_the_validator(tmp_path: Path) -> None:
    mock = MockProvider(sequences={"test-validator": [Script(kind="text", content="Ship it.")]})
    session = _worker_session(tmp_path, mock, _config())

    out = await handle_consult(session=session, question=QUESTION, to="validator")

    assert "Ship it." in out and "validator" in out
    assert [c.model for c in mock.calls] == ["test-validator"]


async def test_consult_is_off_for_a_tier_not_listed(tmp_path: Path) -> None:
    mock = MockProvider()
    session = _worker_session(tmp_path, mock, _config())
    session.acting_tier = "brain"

    with pytest.raises(HandlerRefusal) as refused:
        await handle_consult(session=session, question=QUESTION)

    assert refused.value.code == "consult_off"
    assert mock.calls == []


async def test_worker_switched_off_refuses(tmp_path: Path) -> None:
    mock = MockProvider()
    session = _worker_session(tmp_path, mock, _config(tiers=[]))

    with pytest.raises(HandlerRefusal) as refused:
        await handle_consult(session=session, question=QUESTION)

    assert refused.value.code == "consult_off"
    assert mock.calls == []


async def test_a_brain_that_has_it_asks_the_validator_never_itself(tmp_path: Path) -> None:
    mock = MockProvider(sequences={"test-validator": [Script(kind="text", content="Fine.")]})
    session = _worker_session(tmp_path, mock, _config(tiers=["brain"]))
    session.acting_tier = "brain"

    with pytest.raises(HandlerRefusal) as refused:
        await handle_consult(session=session, question=QUESTION, to="brain")
    assert refused.value.code == "consult_same_tier"

    out = await handle_consult(session=session, question=QUESTION)
    assert "Fine." in out
    assert [c.model for c in mock.calls] == ["test-validator"]


async def test_per_turn_cap_refuses_without_calling(tmp_path: Path) -> None:
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(kind="text", content="one"),
                Script(kind="text", content="two"),
                Script(kind="text", content="three"),
            ]
        }
    )
    session = _worker_session(tmp_path, mock, _config(max_per_turn=2))

    await handle_consult(session=session, question=QUESTION)
    last = await handle_consult(session=session, question=QUESTION)
    assert "Consults left this turn: 0." in last
    with pytest.raises(HandlerRefusal) as refused:
        await handle_consult(session=session, question=QUESTION)

    assert refused.value.code == "consult_cap"
    assert len(mock.calls) == 2


async def test_a_failed_call_still_counts_toward_the_cap(tmp_path: Path) -> None:
    mock = MockProvider(
        sequences={"test-brain": [Script(kind="error", content="down", status_code=400)]}
    )
    session = _worker_session(tmp_path, mock, _config(max_per_turn=1))

    with pytest.raises(HandlerRefusal) as failed:
        await handle_consult(session=session, question=QUESTION)
    assert failed.value.code == "consult_failed"
    with pytest.raises(HandlerRefusal) as capped:
        await handle_consult(session=session, question=QUESTION)
    assert capped.value.code == "consult_cap"


async def test_no_consult_past_the_spend_cap(tmp_path: Path) -> None:
    mock = MockProvider(sequences={"test-brain": [Script(kind="text", content="x")]})
    session = _worker_session(tmp_path, mock, _config())
    session.boundary_config = session.boundary_config.model_copy(
        update={"caps": session.boundary_config.caps.model_copy(update={"spend_usd": 0.0})}
    )

    with pytest.raises(HandlerRefusal) as refused:
        await handle_consult(session=session, question=QUESTION)

    assert refused.value.code == "consult_over_cap"
    assert mock.calls == []


async def test_stop_ends_a_consult_in_flight(tmp_path: Path) -> None:
    mock = MockProvider(sequences={"test-brain": [SLOW_ANSWER]})
    closed = _track_stream_close(mock)
    session = _worker_session(tmp_path, mock, _config())
    await _turn_running(session)
    call = asyncio.create_task(handle_consult(session=session, question=QUESTION))
    await asyncio.sleep(0.3)
    assert not call.done()

    stopped_at = time.monotonic()
    await session.stop_turn()
    with pytest.raises(HandlerRefusal) as refused:
        await asyncio.wait_for(call, 2)

    assert time.monotonic() - stopped_at < 1
    assert refused.value.code == "consult_stopped"
    assert "stopped the turn" in refused.value.message
    # The stream was closed, so the adviser stops generating, and no
    # answer was recorded.
    assert closed == ["test-brain"]
    tracker = session.cost_tracker
    assert tracker is not None
    assert not [c for c in tracker.calls if c.source == "consult"]


async def test_a_consult_that_finishes_before_stop_is_unaffected(tmp_path: Path) -> None:
    mock = MockProvider(sequences={"test-brain": [Script(kind="stream", content="Add it.")]})
    session = _worker_session(tmp_path, mock, _config())
    await _turn_running(session)

    out = await handle_consult(session=session, question=QUESTION)

    assert "Add it." in out
    # The race left no watcher behind to fire on a later Stop.
    await session.stop_turn()
    assert session.stop_turn_requested


async def test_cost_lands_on_the_asked_tier_and_the_meter(tmp_path: Path) -> None:
    mock = MockProvider(sequences={"test-brain": [Script(kind="text", content="ok")]})
    session = _worker_session(tmp_path, mock, _config())
    tracker = session.cost_tracker
    assert tracker is not None
    tracker.begin_turn()

    await handle_consult(session=session, question=QUESTION)

    [record] = tracker.calls
    assert (record.tier, record.model, record.source) == ("brain", "test-brain", "consult")
    assert record.cost > 0
    assert tracker.turn_cost() == record.cost
    updates = [e for e in session.event_log.all_events if isinstance(e, CostUpdate)]
    assert updates and updates[-1].cost_by_tier == {"brain": record.cost}
    assert updates[-1].turn_cost == record.cost


async def test_long_answer_is_capped_in_tokens(tmp_path: Path) -> None:
    mock = MockProvider(sequences={"test-brain": [Script(kind="text", content="word " * 5000)]})
    session = _worker_session(tmp_path, mock, _config())

    out = await handle_consult(session=session, question=QUESTION)

    assert f"capped at {ANSWER_TOKEN_CAP} tokens" in out
    assert len(out) < 5000 * 5


# ── Classifier and policy ───────────────────────────────────────────────


def test_builtin_consult_is_class_a_without_a_model_call(tmp_path: Path) -> None:
    static = DecisionClassifier(Boundary(workspace_root=tmp_path))
    request = build_decision_request(CONSULT_TOOL, {"question": QUESTION})
    decision = static.classify(request)
    assert decision.decision_class is DecisionClass.A
    assert decision.rule is not None and decision.rule.id == "consult"


def test_an_mcp_tool_named_consult_gets_no_grant(tmp_path: Path) -> None:
    static = DecisionClassifier(Boundary(workspace_root=tmp_path))
    impostor = Tool(
        name="consult",
        description="x",
        parameters={"type": "object", "properties": {}},
        provenance="mcp:other",
    )
    request = build_decision_request(impostor, {})
    decision = static.classify(request)
    assert decision.rule is None or decision.rule.id != "consult"


def test_a_policy_rule_can_still_make_consult_ask(tmp_path: Path) -> None:
    policy = PolicyConfig(rules=[PolicyRule(tool="consult", effect="ask")])
    args = {"question": QUESTION}
    assert resolve(PolicyConfig(), CONSULT_TOOL, args, DecisionClass.A, tmp_path) == "auto"
    assert resolve(policy, CONSULT_TOOL, args, DecisionClass.A, tmp_path) == "ask"


# ── The loop: offered per tier, capped per turn ─────────────────────────


async def test_loop_offers_consult_on_worker_turns_only_and_caps_each_turn(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path)
    session = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)

    def consult(n: int) -> Script:
        # Distinct arguments, so the progress guard sees no repeated call.
        args = _consult_args(context=f"{CONTEXT} (attempt {n})")
        return Script(kind="tool_call", tool_name="consult", tool_arguments=args)

    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(kind="stream", content="Planned."),
                Script(kind="text", content="answer one"),
                Script(kind="text", content="answer two"),
                Script(kind="text", content="answer three"),
            ],
            "test-worker": [
                consult(1),
                consult(2),
                consult(3),
                Script(kind="stream", content="Done."),
                consult(4),
                Script(kind="stream", content="Done again."),
            ],
        }
    )
    runner = await start_loop(
        session,
        TierRouter(lead_turns=1),
        mock,
        make_config(),
        registry=dispatcher.registry,
        dispatcher=dispatcher,
    )
    try:
        await session.add_user_message("Plan it")
        await wait_for_turn(session, 1)
        await session.add_user_message("Build it")
        await wait_for_turn(session, 2)
        await session.add_user_message("Once more")
        await wait_for_turn(session, 3)
    finally:
        await runner.cancel()

    brain_turns = _offered(mock, "test-brain")
    assert brain_turns and all("consult" not in names for names in brain_turns)
    worker_turns = _offered(mock, "test-worker")
    assert worker_turns and all("consult" in names for names in worker_turns)

    results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
    assert [r.status for r in results] == ["success", "success", "error", "success"]
    assert "answer one" in results[0].output and "answer two" in results[1].output
    assert results[2].error_code == "consult_cap"
    # A new turn starts a new count.
    assert "answer three" in results[3].output
    # Each consult went to the brain with only its question and context.
    asked = _consult_calls(mock, "test-brain")
    assert len(asked) == 3
    for call in asked:
        text = json.dumps([m.content for m in call.messages])
        assert QUESTION in text and "Build it" not in text
    tracker = session.cost_tracker
    assert tracker is not None
    assert [c.source for c in tracker.calls if c.tier == "brain"].count("consult") == 3


async def test_stop_during_a_consult_ends_the_turn_at_once(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    session = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-brain": [Script(kind="stream", content="Planned."), SLOW_ANSWER],
            "test-worker": [
                Script(kind="tool_call", tool_name="consult", tool_arguments=_consult_args()),
                Script(kind="stream", content="Never reached."),
            ],
        }
    )
    runner = await start_loop(
        session,
        TierRouter(lead_turns=1),
        mock,
        make_config(),
        registry=dispatcher.registry,
        dispatcher=dispatcher,
    )
    try:
        await session.add_user_message("Plan it")
        await wait_for_turn(session, 1)
        await session.add_user_message("Build it")
        for _ in range(200):
            if any(
                isinstance(e, ToolCallEvent) and e.name == "consult"
                for e in session.event_log.all_events
            ):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        stopped_at = time.monotonic()
        await session.stop_turn()
        done = await wait_for_turn(session, 2, _timeout=2.0)
        took = time.monotonic() - stopped_at
    finally:
        await runner.cancel()

    assert took < 1.5, f"Stop waited {took:.1f}s for the consult"
    assert done.error_code == "stopped"
    [result] = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
    assert result.error_code == "consult_stopped"
    # The worker was not called again after Stop.
    assert [c.model for c in mock.calls].count("test-worker") == 1


async def test_loop_with_consult_off_for_the_worker_never_offers_it(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    session = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-brain": [Script(kind="stream", content="Planned.")],
            "test-worker": [Script(kind="stream", content="Done.")],
        }
    )
    config = _config(tiers=[])
    runner = await start_loop(
        session,
        TierRouter(lead_turns=1),
        mock,
        config,
        registry=dispatcher.registry,
        dispatcher=dispatcher,
    )
    try:
        await session.add_user_message("Plan it")
        await wait_for_turn(session, 1)
        await session.add_user_message("Build it")
        await wait_for_turn(session, 2)
    finally:
        await runner.cancel()

    offered = _offered(mock, "test-worker") + _offered(mock, "test-brain")
    assert offered and all("consult" not in names for names in offered)


# ── Delegate children ───────────────────────────────────────────────────


def test_child_registries_add_consult_only_when_asked() -> None:
    parent = create_registry()
    for kind in ("worker", "research"):
        assert "consult" not in {t.name for t in worker_child_registry(parent, kind).list_tools()}
        names = {t.name for t in worker_child_registry(parent, kind, consult=True).list_tools()}
        assert "consult" in names and "delegate" not in names


@pytest.mark.parametrize("kind", ["worker", "research"])
async def test_delegate_child_consults_the_brain_on_the_parents_count(
    tmp_path: Path, kind: str
) -> None:
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-worker": [
                Script(kind="tool_call", tool_name="consult", tool_arguments=_consult_args()),
                Script(kind="tool_call", tool_name="consult", tool_arguments=_consult_args()),
                Script(kind="text", content="Child done."),
            ],
            "test-brain": [Script(kind="text", content="Use the locale.")],
        }
    )
    _bind_runtime(parent, dispatcher, mock)
    # The parent turn already spent one of its two consults.
    parent.consult_tally.used = 1

    summary = await run_worker_child(
        parent=parent,
        task="Fix the cache key",
        kind="worker" if kind == "worker" else "research",
    )

    assert summary == "Child done."
    assert all("consult" in names for names in _offered(mock, "test-worker"))
    assert [c.model for c in _consult_calls(mock, "test-brain")] == ["test-brain"]
    assert parent.consult_tally.used == 2
    # The child's second consult was refused, and said so to the child.
    tool_msgs = [m.content for m in mock.calls[-1].messages if m.role == "tool"]
    assert any("Use the locale." in str(m) for m in tool_msgs)
    assert any("Refused" in str(m) and "consult" in str(m) for m in tool_msgs)
    # The spend reached the parent's meter.
    updates = [e for e in parent.event_log.all_events if isinstance(e, CostUpdate)]
    assert any("brain" in u.cost_by_tier for u in updates)


async def test_delegate_child_without_worker_consult_is_not_offered_it(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(sequences={"test-worker": [Script(kind="text", content="ok")]})
    runtime = _bind_runtime(parent, dispatcher, mock)
    runtime.config = _config(tiers=["brain"])

    await run_worker_child(parent=parent, task="x", kind="research")

    assert _offered(mock, "test-worker") == [
        {"fs_read", "fs_list", "fs_grep", "web_search", "web_fetch"}
    ]


async def test_the_parents_stop_ends_a_childs_consult(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-worker": [
                Script(kind="tool_call", tool_name="consult", tool_arguments=_consult_args()),
                Script(kind="text", content="Never reached."),
            ],
            "test-brain": [SLOW_ANSWER],
        }
    )
    closed = _track_stream_close(mock)
    _bind_runtime(parent, dispatcher, mock)
    await _turn_running(parent)
    child = asyncio.create_task(run_worker_child(parent=parent, task="Fix the cache key"))
    await asyncio.sleep(0.3)
    assert not child.done()

    stopped_at = time.monotonic()
    await parent.stop_turn()
    summary = await asyncio.wait_for(child, 2)

    assert time.monotonic() - stopped_at < 1
    assert summary == "Worker stopped: parent stopped the turn."
    assert closed == ["test-brain"]
    assert [c.model for c in mock.calls].count("test-worker") == 1
