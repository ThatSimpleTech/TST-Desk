"""In-flight context fit (TD-4839).

Cap scales with the tier, parallel reads on a 32k window elide without
splitting tool-call pairs, and a context-window HTTP 400 is retried once
then failed with a short message. Every loop test uses MockProvider.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from tests.test_loop import mock_factory, wait_for_turn
from tests.test_read_tools import make_dispatcher
from tstd.compaction import budget_threshold, estimate_tokens
from tstd.config import ModelConfig, Preset, TierConfig
from tstd.context.tokens import HeuristicTokenCounter, make_token_counter
from tstd.context_fit import (
    elide_tool_text,
    fit_inflight_turn,
    tool_result_char_cap,
    transcript_failure_text,
)
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.protocol import AssistantDelta, ContextCompacted
from tstd.provider import ChatMessage, FunctionCall, content_as_text
from tstd.provider import ToolCall as ProviderToolCall
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.tools.results import truncate_output

_GENERIC_MARKER = "\n\n┈─[truncated — results exceed output cap]─┈"
_ELISION_NOTE = "chars elided — re-read with offset/limit"
# Distinct from the configured 32768 so a leaked upstream body is visible.
_RAW_OVERFLOW = "litellm.ContextWindowExceededError: prompt contains at least 32769 input tokens"
_COUNTER = HeuristicTokenCounter()


def _tier(context_window: int = 32768, max_output_tokens: int = 4096) -> TierConfig:
    return TierConfig(
        slug="ezer-forge",
        base_url="http://mock.local/v1",
        input_price=0.0,
        output_price=0.0,
        cache_read_price=0.0,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )


def _forge_config(context_window: int = 32768, max_output_tokens: int = 4096) -> ModelConfig:
    def tier() -> TierConfig:
        return _tier(context_window, max_output_tokens)

    return ModelConfig(
        presets={"test": Preset(brain=tier(), worker=tier(), validator=tier())},
        active_preset="test",
    )


def _call(call_id: str) -> ProviderToolCall:
    return ProviderToolCall(
        id=call_id,
        type="function",
        function=FunctionCall(name="fs_read", arguments="{}"),
    )


def _tool_turn(bodies: list[str], user: str = "please read") -> list[ChatMessage]:
    calls = [_call(f"c{index}") for index in range(len(bodies))]
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content="system"),
        ChatMessage(role="user", content=user),
        ChatMessage(role="assistant", content=None, tool_calls=calls),
    ]
    for index, body in enumerate(bodies):
        messages.append(ChatMessage(role="tool", content=body, tool_call_id=f"c{index}"))
    return messages


def _assistant_text(session: Session) -> str:
    return "".join(
        event.delta for event in session.event_log.all_events if isinstance(event, AssistantDelta)
    )


async def _run(
    session: Session,
    mock: MockProvider,
    config: ModelConfig,
    workspace: Path,
) -> SessionRunner:
    dispatcher = make_dispatcher(workspace)
    router = TierRouter(lead_turns=8)
    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            router,
            mock_factory(mock),
            config,
            tool_registry=dispatcher.registry,
            tool_dispatcher=dispatcher,
        ),
    )
    await runner.start()
    return runner


# ── Cap ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("window", "max_output", "prefix", "expected"),
    [
        (32768, 4096, 0, 28_672),
        (128_000, 4096, 0, 50_000),
        (1_000_000, 4096, 0, 50_000),
        (32768, 4096, 25_000, 4_000),
        (32768, 4096, 30_000, 4_000),
    ],
)
def test_tool_result_cap_scales_with_the_window(
    window: int, max_output: int, prefix: int, expected: int
) -> None:
    assert tool_result_char_cap(window, max_output, prefix) == expected


def test_generic_marker_is_unchanged() -> None:
    out, truncated = truncate_output("x" * 200, 100, tool_name="big_tool")
    assert truncated is True
    assert out.endswith(_GENERIC_MARKER)
    assert len(out) == 100


def test_positional_truncate_keeps_the_generic_marker() -> None:
    out, truncated = truncate_output("x" * 200, 100)
    assert truncated is True
    assert out.endswith(_GENERIC_MARKER)


def test_fs_read_marker_names_offset_limit() -> None:
    out, truncated = truncate_output("x" * 200, 100, tool_name="fs_read")
    assert truncated is True
    assert "offset/limit" in out
    assert "truncated" in out
    assert len(out) == 100


def test_large_window_cap_matches_today() -> None:
    cap = tool_result_char_cap(128_000, 4096, 0)
    assert cap == 50_000
    body = "q" * 40_000
    out, truncated = truncate_output(body, cap)
    assert truncated is False
    assert out == body
    capped, was = truncate_output("q" * 60_000, cap)
    assert was is True
    assert len(capped) == 50_000


# ── Elision ─────────────────────────────────────────────────────────────


def test_elide_keeps_head_tail_and_counts_the_gap() -> None:
    head = "HEAD-MARKER-"
    tail = "-TAIL-MARKER"
    text = head + ("x" * 8_000) + tail
    out = elide_tool_text(text, 400)
    assert out.startswith(head)
    assert out.endswith(tail)
    assert _ELISION_NOTE in out
    assert len(out) <= 400
    prefix, _, rest = out.partition("\n[")
    removed_text, _, suffix = rest.partition("]\n")
    removed = int(removed_text.split(" ", 1)[0])
    assert text.startswith(prefix)
    assert text.endswith(suffix)
    assert removed == len(text) - len(prefix) - len(suffix)


def test_pairs_and_the_user_message_stay() -> None:
    messages = _tool_turn(["Q" * 40_000, "R" * 40_000, "S" * 40_000, "T" * 40_000])
    fitted, stats = fit_inflight_turn(messages, _tier(), _COUNTER)
    assert stats is not None
    assert [message.role for message in fitted] == [message.role for message in messages]
    assert fitted[1] is messages[1]
    assert fitted[1].content == "please read"
    assert fitted[2] is messages[2]
    assert fitted[2].tool_calls is not None
    ids = [call.id for call in fitted[2].tool_calls]
    assert [message.tool_call_id for message in fitted[3:]] == ids
    tokens, _ = estimate_tokens(fitted, _COUNTER)
    assert tokens <= budget_threshold(_tier())
    assert stats.elided_results >= 1
    assert stats.kept_messages == len(fitted)
    assert stats.tokens_before > stats.tokens_after
    assert _ELISION_NOTE in content_as_text(fitted[3].content)


def test_older_tool_result_is_untouched() -> None:
    old_body = "UNTOUCHED-" + ("o" * 200)
    old_tool = ChatMessage(role="tool", content=old_body, tool_call_id="old")
    messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content=None, tool_calls=[_call("old")]),
        old_tool,
        ChatMessage(role="user", content="second"),
        ChatMessage(role="assistant", content=None, tool_calls=[_call("new")]),
        ChatMessage(role="tool", content="N" * 120_000, tool_call_id="new"),
    ]
    fitted, stats = fit_inflight_turn(messages, _tier(), _COUNTER)
    assert stats is not None
    assert fitted[3] is old_tool
    assert fitted[3].content == old_body
    assert fitted[1].content == "first"
    assert fitted[4].content == "second"
    assert _ELISION_NOTE in content_as_text(fitted[-1].content)


def test_image_part_survives_elision() -> None:
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}
    messages = [
        ChatMessage(role="user", content="look"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[_call("c0")],
        ),
        ChatMessage(
            role="tool",
            content=[{"type": "text", "text": "T" * 120_000}, image],
            tool_call_id="c0",
        ),
    ]
    fitted, stats = fit_inflight_turn(messages, _tier(), _COUNTER)
    assert stats is not None
    parts = fitted[-1].content
    assert isinstance(parts, list)
    assert image in parts
    texts = [part["text"] for part in parts if part.get("type") == "text"]
    assert texts == [text for text in texts if _ELISION_NOTE in text]
    assert len(texts) == 1


def test_one_result_counts_once_when_shrunk_twice() -> None:
    messages = _tool_turn(["Z" * 200_000])
    _fitted, stats = fit_inflight_turn(messages, _tier(), _COUNTER)
    assert stats is not None
    assert stats.elided_results == 1
    assert stats.kept_messages == len(messages)


def test_half_budget_is_strictly_smaller() -> None:
    tier = _tier()
    messages = _tool_turn(["A" * 40_000, "B" * 40_000, "C" * 40_000, "D" * 40_000])
    fitted, full = fit_inflight_turn(messages, tier, _COUNTER)
    assert full is not None
    half_budget = max(1, budget_threshold(tier) // 2)
    assert full.tokens_after > half_budget
    _tighter, half = fit_inflight_turn(fitted, tier, _COUNTER, budget=half_budget)
    assert half is not None
    assert half.tokens_after < full.tokens_after
    assert half.tokens_after <= half_budget


def test_large_window_does_not_elide_results_under_the_ceiling() -> None:
    messages = _tool_turn(["z" * 40_000] * 4)
    fitted, stats = fit_inflight_turn(messages, _tier(128_000, 4096), _COUNTER)
    assert stats is None
    assert fitted is messages


def test_overflow_message_names_config_not_the_provider() -> None:
    text = transcript_failure_text(
        "context_overflow",
        _RAW_OVERFLOW,
        model="ezer-forge",
        context_window=32768,
    )
    assert "ezer-forge" in text
    assert "32768-token" in text
    assert "litellm" not in text
    assert "32769" not in text


def test_raw_json_is_not_assistant_prose() -> None:
    text = transcript_failure_text(
        "bad_request",
        '{"error":"RAWJSONTOKEN"}',
        model="ezer-forge",
        context_window=32768,
    )
    assert text == "I encountered an error: The provider rejected the request."
    assert "RAWJSONTOKEN" not in text


def test_clean_provider_prose_is_kept() -> None:
    text = transcript_failure_text(
        "auth_failed",
        "Authentication failed.",
        model="ezer-forge",
        context_window=32768,
    )
    assert text == "I encountered an error: Authentication failed."


# ── Loop ────────────────────────────────────────────────────────────────


def _write_reads(workspace: Path) -> tuple[tuple[str, str], ...]:
    names = ("readme.md", "spec.md", "architecture.md", "backlog.md")
    for name in names:
        line = f"{name}-" + ("x" * 60)
        (workspace / name).write_text("\n".join([line] * 1200) + "\n", encoding="utf-8")
    return tuple(("fs_read", json.dumps({"path": name})) for name in names)


def _overflow_script() -> Script:
    return Script(
        kind="error",
        error_code="context_overflow",
        status_code=400,
        content=_RAW_OVERFLOW,
    )


async def test_parallel_reads_fit_a_32k_tier(tmp_path: Path) -> None:
    batch = _write_reads(tmp_path)
    config = _forge_config()
    mock = MockProvider(
        sequences={
            "ezer-forge": [
                Script(kind="tool_call", tool_batch=batch),
                Script(kind="stream", content="Here is the page."),
            ]
        }
    )
    session = Session(str(tmp_path))
    runner = await _run(session, mock, config, tmp_path)
    try:
        await session.add_user_message("Write an HTML page about the project")
        turn = await wait_for_turn(session, 1, 15.0)
    finally:
        await runner.cancel()

    assert turn.failed is False
    assert len(mock.calls) == 2
    sent = list(mock.calls[1].messages)
    roles = [message.role for message in sent]
    assistant_at = roles.index("assistant")
    assert roles[assistant_at : assistant_at + 5] == [
        "assistant",
        "tool",
        "tool",
        "tool",
        "tool",
    ]
    assistant = sent[assistant_at]
    assert assistant.tool_calls is not None
    ids = [call.id for call in assistant.tool_calls]
    assert ids == [f"call_mock_{index}" for index in range(1, 5)]
    assert [message.tool_call_id for message in sent[assistant_at + 1 : assistant_at + 5]] == ids
    user = next(message for message in sent if message.role == "user")
    assert user.content == "Write an HTML page about the project"
    assert any(
        _ELISION_NOTE in content_as_text(message.content)
        for message in sent
        if message.role == "tool"
    )
    for message in sent:
        if message.role == "tool":
            assert len(content_as_text(message.content)) <= 28_672
    tokens, _ = estimate_tokens(sent, make_token_counter("ezer-forge"))
    assert tokens <= budget_threshold(config.tier("brain"))
    events = [
        event for event in session.event_log.all_events if isinstance(event, ContextCompacted)
    ]
    assert len(events) == 1
    assert events[0].dropped_messages >= 1
    assert events[0].kept_messages == len(sent)


async def test_overflow_retries_once_then_fails_clean(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _forge_config()
    mock = MockProvider(sequences={"ezer-forge": [_overflow_script(), _overflow_script()]})
    session = Session(str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="tstd.loop"):
        runner = await _run(session, mock, config, tmp_path)
        try:
            await session.add_user_message("hi")
            turn = await wait_for_turn(session, 1, 10.0)
        finally:
            await runner.cancel()

    assert len(mock.calls) == 2
    assert turn.failed is True
    assert turn.error_code == "context_overflow"
    shown = _assistant_text(session)
    assert "ezer-forge" in shown
    assert "32768" in shown
    assert "32769" not in shown
    assert "litellm" not in shown.lower()
    assert "ContextWindowExceededError" not in shown
    for message in session.conversation:
        text = content_as_text(message.content)
        assert "32769" not in text
        assert "litellm" not in text.lower()
    logged = [
        str(getattr(record, "extra_fields", {}).get("error", ""))
        for record in caplog.records
        if record.name == "tstd.loop" and record.getMessage() == "provider context overflow"
    ]
    assert any(_RAW_OVERFLOW in item for item in logged)


async def test_overflow_retry_elides_to_half_the_budget(tmp_path: Path) -> None:
    batch = _write_reads(tmp_path)
    config = _forge_config()
    mock = MockProvider(
        sequences={
            "ezer-forge": [
                Script(kind="tool_call", tool_batch=batch),
                _overflow_script(),
                _overflow_script(),
            ]
        }
    )
    session = Session(str(tmp_path))
    runner = await _run(session, mock, config, tmp_path)
    try:
        await session.add_user_message("Write an HTML page about the project")
        turn = await wait_for_turn(session, 1, 15.0)
    finally:
        await runner.cancel()

    assert len(mock.calls) == 3
    assert turn.failed is True
    assert turn.error_code == "context_overflow"
    counter = make_token_counter("ezer-forge")
    first, _ = estimate_tokens(list(mock.calls[1].messages), counter)
    second, _ = estimate_tokens(list(mock.calls[2].messages), counter)
    half = max(1, budget_threshold(config.tier("brain")) // 2)
    assert second <= half
    assert second < first
    shown = _assistant_text(session)
    assert "32769" not in shown
    assert "litellm" not in shown.lower()
    assert "ezer-forge" in shown


async def test_raw_upstream_json_is_not_assistant_text(tmp_path: Path) -> None:
    raw = '{"error":"RAWJSONTOKEN","message":"temperature must be between 0 and 2"}'
    mock = MockProvider(
        sequences={
            "ezer-forge": [
                Script(kind="error", error_code="bad_request", status_code=400, content=raw)
            ]
        }
    )
    session = Session(str(tmp_path))
    runner = await _run(session, mock, config := _forge_config(), tmp_path)
    try:
        await session.add_user_message("hi")
        turn = await wait_for_turn(session, 1, 10.0)
    finally:
        await runner.cancel()

    assert turn.failed is True
    assert turn.error_code == "bad_request"
    assert config.tier("brain").context_window == 32768
    shown = _assistant_text(session)
    assert "RAWJSONTOKEN" not in shown
    assert "The provider rejected the request." in shown
    for message in session.conversation:
        assert "RAWJSONTOKEN" not in content_as_text(message.content)
