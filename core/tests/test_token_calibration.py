"""True token accounting (TD-5007).

The estimate counts tool definitions and per-message template overhead,
and learns a per-endpoint ratio from ``usage.prompt_tokens``. Loop tests
use a MockProvider that reports 1.5x the raw estimate of what it was
sent, the way EZER's local model ran against the 4 chars/token guess.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.test_context_fit import _forge_config, _tier, _tool_turn
from tests.test_loop import mock_factory, wait_for_turn
from tests.test_read_tools import make_dispatcher
from tstd import loop as loop_module
from tstd.compaction import (
    MESSAGE_OVERHEAD_TOKENS,
    CompactionStats,
    budget_threshold,
    estimate_tokens,
)
from tstd.compaction_model import compact_auto
from tstd.config import ModelConfig, TierConfig
from tstd.context.calibration import (
    EMA_ALPHA,
    MAX_RATIO,
    MIN_RATIO,
    MIN_RAW_TOKENS,
    CalibratedTokenCounter,
    CalibrationStore,
    effective_chars_per_token,
    process_calibration_store,
)
from tstd.context.tokens import APPROX_METHOD, CHARS_PER_TOKEN, HeuristicTokenCounter, TokenCounter
from tstd.context_fit import InTurnFit, fit_inflight_turn, tool_result_char_cap
from tstd.mock import MockProvider, Script
from tstd.protocol import ContextCompacted
from tstd.provider import (
    ChatCompletionRequest,
    ChatMessage,
    FunctionDefinition,
    ProviderError,
    StreamChunk,
    ToolDefinition,
    content_as_text,
)
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner

_BASE = HeuristicTokenCounter()
_KEY = ("http://mock.local/v1", "ezer-forge")
_REPORTED_RATIO = 1.5


def _tool(name: str = "fs_read") -> ToolDefinition:
    return ToolDefinition(
        type="function",
        function=FunctionDefinition(
            name=name,
            description="Read a file in the workspace, with offset and limit.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}},
                "required": ["path"],
            },
        ),
    )


def _calibrated(ratio: float | None) -> CalibratedTokenCounter:
    store = CalibrationStore()
    if ratio is not None:
        store.observe(_KEY, 10_000, round(10_000 * ratio))
    return CalibratedTokenCounter(_BASE, store, _KEY)


# ── What the estimate counts ────────────────────────────────────────────


def test_tool_definitions_count_as_the_request_serializes_them() -> None:
    messages = [ChatMessage(role="user", content="hi")]
    tools = [_tool("fs_read"), _tool("fs_write")]
    without, _ = estimate_tokens(messages, _BASE)
    with_tools, method = estimate_tokens(messages, _BASE, tools)
    sent = ChatCompletionRequest(model="m", messages=messages, tools=tools).to_dict()["tools"]
    expected = sum(_BASE.count(json.dumps(payload)).count for payload in sent)
    assert with_tools - without == expected
    assert method == APPROX_METHOD


def test_each_message_carries_template_overhead() -> None:
    empty = [ChatMessage(role="assistant", content=None) for _ in range(5)]
    assert estimate_tokens(empty, _BASE) == (5 * MESSAGE_OVERHEAD_TOKENS, "")
    one = [ChatMessage(role="user", content="x" * 40)]
    assert estimate_tokens(one, _BASE)[0] == 10 + MESSAGE_OVERHEAD_TOKENS


def test_overhead_and_tools_scale_with_the_ratio() -> None:
    messages = [ChatMessage(role="user", content="x" * 400) for _ in range(3)]
    tools = [_tool()]
    raw, _ = estimate_tokens(messages, _BASE, tools)
    scaled, method = estimate_tokens(messages, _calibrated(2.0), tools)
    assert scaled == 2 * raw
    assert "x2.00 calibrated" in method


# ── Calibration math ────────────────────────────────────────────────────


def test_first_observation_replaces_the_seed() -> None:
    store = CalibrationStore()
    assert store.ratio(_KEY) is None
    assert store.observe(_KEY, 1_000, 1_550) == pytest.approx(1.55)
    assert store.ratio(_KEY) == pytest.approx(1.55)


def test_later_observations_fold_in_by_ema() -> None:
    store = CalibrationStore()
    store.observe(_KEY, 1_000, 1_500)
    updated = store.observe(_KEY, 1_000, 2_000)
    assert updated == pytest.approx(1.5 + EMA_ALPHA * (2.0 - 1.5))
    assert store.ratio(_KEY) == pytest.approx(1.65)


@pytest.mark.parametrize(
    ("prompt_tokens", "expected"),
    [(100_000, MAX_RATIO), (10, MIN_RATIO)],
)
def test_samples_clamp_before_folding(prompt_tokens: int, expected: float) -> None:
    store = CalibrationStore()
    assert store.observe(_KEY, 1_000, prompt_tokens) == expected


@pytest.mark.parametrize(
    ("raw", "prompt_tokens"),
    [(MIN_RAW_TOKENS - 1, 5_000), (1_000, None), (1_000, 0), (1_000, -3)],
)
def test_untrustworthy_observations_are_ignored(raw: int, prompt_tokens: int | None) -> None:
    store = CalibrationStore()
    store.observe(_KEY, 1_000, 1_200)
    assert store.observe(_KEY, raw, prompt_tokens) is None
    assert store.ratio(_KEY) == pytest.approx(1.2)


def test_ratios_are_per_endpoint_and_model() -> None:
    store = CalibrationStore()
    store.observe(("http://a/v1", "m"), 1_000, 1_500)
    assert store.ratio(("http://b/v1", "m")) is None
    assert store.ratio(("http://a/v1", "other")) is None
    store.clear()
    assert store.ratio(("http://a/v1", "m")) is None


def test_process_store_is_shared() -> None:
    assert process_calibration_store() is process_calibration_store()


def test_method_names_the_ratio_only_once_calibrated() -> None:
    counter = _calibrated(None)
    plain = counter.count("x" * 400)
    assert plain.count == 100
    assert plain.method == APPROX_METHOD
    counter.observe(1_000, 1_500)
    scaled = counter.count("x" * 401)
    assert scaled.count == math.ceil(101 * 1.5)
    assert scaled.method == f"{APPROX_METHOD} x1.50 calibrated from provider usage"


def test_effective_chars_per_token_follows_the_ratio() -> None:
    assert effective_chars_per_token(_BASE) == CHARS_PER_TOKEN
    assert effective_chars_per_token(_calibrated(None)) == CHARS_PER_TOKEN
    assert effective_chars_per_token(_calibrated(1.6)) == pytest.approx(2.5)


def test_result_cap_converts_with_the_calibrated_ratio() -> None:
    plain = tool_result_char_cap(32_768, 4_096, 2_000)
    assert plain == tool_result_char_cap(32_768, 4_096, 2_000, CHARS_PER_TOKEN)
    calibrated = tool_result_char_cap(32_768, 4_096, 2_000, 2.5)
    assert calibrated < plain
    assert calibrated == math.floor(((32_768 - 4_096 - 2_000) // 4) * 2.5)


def test_fit_converts_excess_with_the_calibrated_ratio() -> None:
    """One pass lands under the calibrated budget, not 1.6x over it."""
    tier = _tier()
    counter = _calibrated(1.6)
    messages = _tool_turn(["Q" * 70_000])
    fitted, stats = fit_inflight_turn(messages, tier, counter)
    assert isinstance(stats, InTurnFit)
    assert stats.tokens_after <= budget_threshold(tier)
    assert "x1.60 calibrated" in stats.counter_method
    tokens, _ = estimate_tokens(fitted, counter)
    assert tokens == stats.tokens_after


def test_observer_uses_the_raw_estimate_so_ratios_do_not_compound() -> None:
    counter = _calibrated(1.5)
    messages = [ChatMessage(role="user", content="x" * 4_000)]
    tools = [_tool()]
    raw, _ = estimate_tokens(messages, _BASE, tools)
    observe = loop_module._calibration_observer(counter, messages, tools)
    assert observe is not None
    observe(Script(kind="text", prompt_tokens=round(raw * 1.5)).usage)
    assert counter.ratio == pytest.approx(1.5, rel=1e-3)


def test_image_bearing_calls_teach_nothing() -> None:
    counter = _calibrated(None)
    messages = [
        ChatMessage(role="user", content="x" * 4_000),
        ChatMessage(
            role="tool",
            tool_call_id="c0",
            content=[
                {"type": "text", "text": "screenshot"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        ),
    ]
    assert loop_module._calibration_observer(counter, messages, None) is None


def test_calls_echoing_reasoning_details_teach_nothing() -> None:
    """An encrypted reasoning blob's length is not its token cost (TD-1903)."""
    counter = _calibrated(None)
    details = [{"type": "reasoning.encrypted", "data": "Z" * 4_000, "index": 0}]
    messages = [
        ChatMessage(role="user", content="x" * 4_000),
        ChatMessage(role="assistant", content="done", reasoning_details=details),
    ]
    assert loop_module._calibration_observer(counter, messages, None) is None


# ── Loop: the next call sees calibrated counts ──────────────────────────


class _ScaledUsageProvider(MockProvider):
    """Reports ``prompt_tokens`` as 1.5x the raw estimate of each request."""

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[StreamChunk | ProviderError]:
        raw, _ = estimate_tokens(list(request.messages), _BASE, request.tools)
        reported = math.ceil(raw * _REPORTED_RATIO)
        async for chunk in super().chat_completion_stream(request):
            if isinstance(chunk, StreamChunk) and chunk.usage is not None:
                chunk = replace(chunk, usage=replace(chunk.usage, prompt_tokens=reported))
            yield chunk


async def _run(
    session: Session,
    mock: MockProvider,
    config: ModelConfig,
    workspace: Path,
    store: CalibrationStore | None,
) -> SessionRunner:
    dispatcher = make_dispatcher(workspace)
    runner = SessionRunner(
        session,
        loop_factory=lambda s: loop_module.agent_loop(
            s,
            TierRouter(lead_turns=8),
            mock_factory(mock),
            config,
            tool_registry=dispatcher.registry,
            tool_dispatcher=dispatcher,
            calibration_store=store,
        ),
    )
    await runner.start()
    return runner


_READS = 5
# Sized so the whole turn sits well inside the 41k test window by the raw
# estimate (about 27k tokens with today's ~41 tool definitions) yet over it
# at the 1.5 calibrated ratio. 20k chars a read left no room: one more
# builtin tool (TD-5121 ask_user beside TD-5119 consult) pushed the raw
# estimate past the budget and the control turn elided too.
_READ_CHARS = 16_000


def _reads(workspace: Path) -> tuple[tuple[str, str], ...]:
    names = [f"part{index}.md" for index in range(_READS)]
    for name in names:
        (workspace / name).write_text("y" * _READ_CHARS, encoding="utf-8")
    return tuple(("fs_read", json.dumps({"path": name})) for name in names)


def _two_turns(workspace: Path) -> MockProvider:
    return _ScaledUsageProvider(
        sequences={
            "ezer-forge": [
                Script(kind="stream", content="hello"),
                Script(kind="tool_call", tool_batch=_reads(workspace)),
                Script(kind="stream", content="read them"),
            ]
        }
    )


async def _drive(session: Session, runner: SessionRunner) -> None:
    try:
        await session.add_user_message("hi")
        await wait_for_turn(session, 1, 10.0)
        await session.add_user_message("read the parts")
        await wait_for_turn(session, 2, 15.0)
    finally:
        await runner.cancel()


def _tool_texts(request: ChatCompletionRequest) -> list[str]:
    return [content_as_text(m.content) for m in request.messages if m.role == "tool"]


async def test_compaction_and_fit_see_the_learned_ratio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, int, int]] = []
    real_compact = compact_auto
    real_fit = fit_inflight_turn

    def _record(
        stage: str,
        messages: list[ChatMessage],
        counter: TokenCounter,
        tools: Sequence[ToolDefinition] | None,
    ) -> None:
        assert isinstance(counter, CalibratedTokenCounter)
        scaled, _ = estimate_tokens(messages, counter, tools)
        raw, _ = estimate_tokens(messages, counter.base, tools)
        seen.append((stage, raw, scaled))

    async def _compact(
        messages: list[ChatMessage],
        tier: TierConfig,
        counter: TokenCounter,
        tools: Sequence[ToolDefinition] | None,
        **kwargs: Any,
    ) -> tuple[list[ChatMessage], CompactionStats | None]:
        _record("compact", messages, counter, tools)
        return await real_compact(messages, tier, counter, tools, **kwargs)

    def _fit(
        messages: list[ChatMessage], tier: TierConfig, counter: TokenCounter, **kwargs: Any
    ) -> tuple[list[ChatMessage], InTurnFit | None]:
        _record("fit", messages, counter, kwargs.get("tools"))
        return real_fit(messages, tier, counter, **kwargs)

    monkeypatch.setattr(loop_module, "compact_auto", _compact)
    monkeypatch.setattr(loop_module, "fit_inflight_turn", _fit)
    store = CalibrationStore()
    session = Session(str(tmp_path))
    runner = await _run(session, _two_turns(tmp_path), _forge_config(), tmp_path, store)
    await _drive(session, runner)

    assert store.ratio(_KEY) == pytest.approx(_REPORTED_RATIO, rel=0.01)
    first_compact, first_fit = seen[0], seen[1]
    assert first_compact[0] == "compact" and first_fit[0] == "fit"
    assert first_compact[1] > MIN_RAW_TOKENS
    assert first_compact[2] == first_compact[1]
    assert first_fit[2] == first_fit[1]
    # Every later call is estimated at the learned ratio.
    for stage, raw, scaled in seen[2:]:
        assert scaled == pytest.approx(raw * _REPORTED_RATIO, rel=0.01), stage


async def test_calibrated_ratio_elides_what_the_raw_estimate_lets_through(
    tmp_path: Path,
) -> None:
    config = _forge_config(context_window=41_000, max_output_tokens=1_000)
    budget = budget_threshold(config.tier("brain"))

    control_dir = tmp_path / "control"
    control_dir.mkdir()
    control = _two_turns(control_dir)
    control_session = Session(str(control_dir))
    await _drive(control_session, await _run(control_session, control, config, control_dir, None))
    raw_full, _ = estimate_tokens(list(control.calls[2].messages), _BASE, control.calls[2].tools)
    assert raw_full <= budget < raw_full * _REPORTED_RATIO
    assert all(len(text) >= _READ_CHARS for text in _tool_texts(control.calls[2]))
    assert not [e for e in control_session.event_log.all_events if isinstance(e, ContextCompacted)]

    calibrated_dir = tmp_path / "calibrated"
    calibrated_dir.mkdir()
    store = CalibrationStore()
    mock = _two_turns(calibrated_dir)
    session = Session(str(calibrated_dir))
    await _drive(session, await _run(session, mock, config, calibrated_dir, store))

    assert len(mock.calls) == 3
    assert len(_tool_texts(mock.calls[2])) == _READS
    events = [e for e in session.event_log.all_events if isinstance(e, ContextCompacted)]
    assert len(events) == 1
    # Counts at the ratio the fit used; call 3 has since nudged the store.
    assert events[0].tokens_before > budget
    assert events[0].tokens_after <= budget
    assert sum(len(text) for text in _tool_texts(mock.calls[2])) < _READS * _READ_CHARS


async def test_turn_start_log_carries_the_ratio(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = CalibrationStore()
    session = Session(str(tmp_path))
    with caplog.at_level(logging.INFO, logger="tstd.loop"):
        runner = await _run(session, _two_turns(tmp_path), _forge_config(), tmp_path, store)
        await _drive(session, runner)
    ratios = [
        getattr(record, "extra_fields", {}).get("token_ratio", "missing")
        for record in caplog.records
        if record.name == "tstd.loop" and record.getMessage() == "turn start"
    ]
    assert len(ratios) == 3
    assert ratios[0] is None
    assert ratios[1] == pytest.approx(_REPORTED_RATIO, rel=0.01)
    assert ratios[2] == pytest.approx(_REPORTED_RATIO, rel=0.01)


async def test_a_provider_without_usage_leaves_the_estimate_raw(tmp_path: Path) -> None:
    store = CalibrationStore()
    mock = MockProvider(
        sequences={
            "ezer-forge": [
                Script(kind="stream", content="hello", prompt_tokens=0),
                Script(kind="stream", content="again", prompt_tokens=0),
            ]
        }
    )
    session = Session(str(tmp_path))
    runner = await _run(session, mock, _forge_config(), tmp_path, store)
    try:
        await session.add_user_message("hi")
        await wait_for_turn(session, 1, 10.0)
        await session.add_user_message("again")
        await wait_for_turn(session, 2, 10.0)
    finally:
        await runner.cancel()
    assert store.ratio(_KEY) is None
