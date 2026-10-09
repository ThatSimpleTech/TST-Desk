"""Replies cut off at max_output_tokens (TD-5008).

A turn request carries ``max_tokens``, so a long reply can stop with
``finish_reason: "length"``. Cut text is kept. A cut tool call is not
run, and the transcript replays it with empty arguments and a note
telling the model to send smaller pieces, never as invalid JSON.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from tests.test_cap_enforcement import make_echo_session
from tests.test_delegate import _parent_dispatcher, _workspace, _zero_arg_factory
from tests.test_dispatch import start_loop as start_dispatch_loop
from tests.test_loop import wait_for_turn
from tests.test_request_extras import _BRAIN_MAX, _WORKER_MAX, _config
from tstd.boundary_config import BoundaryConfig
from tstd.context import PromptAssembler
from tstd.cost import CostTracker
from tstd.mock import MockProvider, Script
from tstd.output_cap import CUT_CALL_ARGUMENTS, arguments_complete, cut_call_note, split_cut_calls
from tstd.protocol import AssistantDelta, ToolResult
from tstd.provider import ChatCompletionRequest, ChatMessage, FunctionCall, ToolCall
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools.delegate import DelegateRuntime, run_worker_child

_CUT_WRITE = '{"path": "page.html", "content": "<html><body><h1>Proj'


def _cap_warnings(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        record.extra_fields  # type: ignore[attr-defined]
        for record in caplog.records
        if record.getMessage() == "reply hit the output cap"
    ]


def _assert_replayable(requests: list[ChatCompletionRequest]) -> None:
    """Every tool call any request replays is a whole JSON object."""
    for request in requests:
        for message in request.messages:
            for call in message.tool_calls or []:
                assert call.function is not None
                assert isinstance(json.loads(call.function.arguments), dict)


def _turn_calls(mock: MockProvider) -> list[ChatCompletionRequest]:
    """The brain's turn requests; the classifier's worker calls left out."""
    return [call for call in mock.calls if call.model == "test-brain"]


def _call(call_id: str, arguments: str) -> ToolCall:
    return ToolCall(
        id=call_id, type="function", function=FunctionCall(name="fs_write", arguments=arguments)
    )


# ── Unit ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("arguments", "complete"),
    [
        ('{"message": "hi"}', True),
        ("{}", True),
        (_CUT_WRITE, False),
        ('{"message": "hi"', False),
        ("", False),
        ("[1]", False),
    ],
)
def test_arguments_complete(arguments: str, complete: bool) -> None:
    assert arguments_complete(arguments) is complete


def test_split_keeps_order_within_each_side() -> None:
    calls = [_call("a", "{}"), _call("b", _CUT_WRITE), _call("c", '{"path": "x"}')]
    complete, cut = split_cut_calls(calls)
    assert [c.id for c in complete] == ["a", "c"]
    assert [c.id for c in cut] == ["b"]


def test_note_names_the_cap_and_the_way_out() -> None:
    note = cut_call_note(2048)
    assert "max_output_tokens limit (2048)" in note
    assert "fs_write a skeleton, then fs_edit" in note


# ── Loop ────────────────────────────────────────────────────────────────


async def test_cut_tool_call_is_not_run_or_replayed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session, dispatcher = make_echo_session(tmp_path, BoundaryConfig())
    batch = (("echo", '{"message": "first"}'), ("echo", '{"message": "second, and lo'))
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(kind="tool_call", tool_batch=batch, finish_reason="length"),
                Script(kind="stream", content="Sent the first one."),
            ]
        }
    )
    runner = await start_dispatch_loop(
        session, TierRouter(lead_turns=80), mock, _config(), None, dispatcher
    )
    try:
        with caplog.at_level(logging.WARNING, logger="tstd.output_cap"):
            await session.add_user_message("echo twice")
            turn = await wait_for_turn(session, 1)
    finally:
        await runner.cancel()

    assert turn.failed is False
    results = [e for e in session.event_log.all_events if isinstance(e, ToolResult)]
    assert [r.tool_call_id for r in results] == ["call_mock_1"]

    turn_calls = _turn_calls(mock)
    assert len(turn_calls) == 2
    replay = turn_calls[1].messages
    assistant = next(m for m in replay if m.tool_calls)
    assert [
        (c.id, c.function.arguments if c.function else None) for c in assistant.tool_calls or []
    ] == [
        ("call_mock_1", '{"message": "first"}'),
        ("call_mock_2", CUT_CALL_ARGUMENTS),
    ]
    replies = {m.tool_call_id: m.content for m in replay if m.role == "tool"}
    assert replies["call_mock_1"] == "Echo: first"
    assert replies["call_mock_2"] == cut_call_note(_BRAIN_MAX)
    _assert_replayable(turn_calls)

    assert _cap_warnings(caplog) == [
        {
            "session_id": session.id,
            "tier": "brain",
            "model": "test-brain",
            "max_output_tokens": _BRAIN_MAX,
            "cut_tool_calls": 1,
        }
    ]


async def test_only_cut_calls_keep_the_turn_going(tmp_path: Path) -> None:
    session, dispatcher = make_echo_session(tmp_path, BoundaryConfig())
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(
                    kind="tool_call",
                    tool_name="echo",
                    tool_arguments='{"message": "a very long',
                    finish_reason="length",
                ),
                Script(kind="tool_call", tool_name="echo", tool_arguments='{"message": "short"}'),
                Script(kind="stream", content="Done."),
            ]
        }
    )
    runner = await start_dispatch_loop(
        session, TierRouter(lead_turns=80), mock, _config(), None, dispatcher
    )
    try:
        await session.add_user_message("echo something")
        turn = await wait_for_turn(session, 1)
    finally:
        await runner.cancel()

    assert turn.failed is False
    turn_calls = _turn_calls(mock)
    assert len(turn_calls) == 3
    results = [e for e in session.event_log.all_events if isinstance(e, ToolResult)]
    assert len(results) == 1
    retried = turn_calls[1].messages
    assert retried[-1] == ChatMessage(
        role="tool", content=cut_call_note(_BRAIN_MAX), tool_call_id="call_mock_1"
    )
    _assert_replayable(turn_calls)


async def test_text_cut_at_the_cap_is_kept_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session = Session(str(tmp_path))
    mock = MockProvider(
        scripts={
            "test-brain": Script(kind="stream", content="The answer is", finish_reason="length")
        }
    )
    runner = await start_dispatch_loop(session, TierRouter(lead_turns=80), mock, _config())
    try:
        with caplog.at_level(logging.WARNING, logger="tstd.output_cap"):
            await session.add_user_message("explain")
            turn = await wait_for_turn(session, 1)
    finally:
        await runner.cancel()

    assert turn.failed is False
    text = "".join(e.delta for e in session.event_log.all_events if isinstance(e, AssistantDelta))
    assert text == "The answer is"
    assert [w["cut_tool_calls"] for w in _cap_warnings(caplog)] == [0]


# ── Delegate worker ─────────────────────────────────────────────────────


async def test_delegate_worker_does_not_run_a_cut_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
    batch = (("fs_read", '{"path": "notes.md"}'), ("fs_write", '{"path": "out.md", "content": "ab'))
    mock = MockProvider(
        sequences={
            "test-worker": [
                Script(kind="tool_call", tool_batch=batch, finish_reason="length"),
                Script(kind="text", content="notes.md says hello; out.md not written yet"),
            ]
        }
    )
    config = _config()
    parent.cost_tracker = CostTracker(config)
    parent.delegate_runtime = DelegateRuntime(
        provider_factory=_zero_arg_factory(mock),
        config=config,
        dispatcher=_parent_dispatcher(ws),
        assembler=PromptAssembler(parent.workspace_path),
        session_start=time.time(),
        iterations=0,
    )

    with caplog.at_level(logging.WARNING, logger="tstd.output_cap"):
        summary = await run_worker_child(parent=parent, task="Read notes.md, write out.md")

    assert "hello" in summary
    assert not (ws / "out.md").exists()
    worker_calls = [call for call in mock.calls if call.model == "test-worker"]
    assert len(worker_calls) == 2
    replay = worker_calls[1].messages
    replies = {m.tool_call_id: m.content for m in replay if m.role == "tool"}
    assert replies["call_mock_2"] == cut_call_note(_WORKER_MAX)
    assert "hello from notes" in str(replies["call_mock_1"])
    _assert_replayable(worker_calls)
    assert [(w["tier"], w["cut_tool_calls"]) for w in _cap_warnings(caplog)] == [("worker", 1)]
