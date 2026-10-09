"""Elision hysteresis (TD-5010).

In-turn elision triggers at the compaction budget but shrinks the turn's
tool results to ``ELIDE_TARGET_FRACTION`` of it. The next small result
then fits without rewriting results already sent, so a local server's
prefix cache survives. When the rest of the prompt leaves no room for
that, the target rises toward the budget instead of cutting every
result to the floor. The overflow retry still halves the budget.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_context_fit import (
    _ELISION_NOTE,
    _assistant_text,
    _call,
    _forge_config,
    _overflow_script,
    _run,
    _tier,
    _tool_turn,
    _write_reads,
)
from tests.test_loop import wait_for_turn
from tests.test_token_calibration import _calibrated
from tstd import context_fit
from tstd.compaction import budget_threshold, estimate_tokens
from tstd.context.tokens import HeuristicTokenCounter
from tstd.context_fit import ELIDE_TARGET_FRACTION, elision_target, fit_inflight_turn
from tstd.mock import MockProvider, Script
from tstd.protocol import ContextCompacted
from tstd.provider import (
    ChatCompletionRequest,
    ChatMessage,
    FunctionCall,
    ToolCall,
    content_as_text,
)
from tstd.session import Session

_COUNTER = HeuristicTokenCounter()


def _target(budget: int) -> int:
    return max(1, int(budget * ELIDE_TARGET_FRACTION))


def _with_small_result(messages: list[ChatMessage], call_id: str = "c9") -> list[ChatMessage]:
    """The same turn, one more tool round with a small result."""
    return [
        *messages,
        ChatMessage(role="assistant", content=None, tool_calls=[_call(call_id)]),
        ChatMessage(role="tool", content="s" * 2_000, tool_call_id=call_id),
    ]


# ── Unit ────────────────────────────────────────────────────────────────


def test_target_fraction_is_seventy_percent() -> None:
    assert ELIDE_TARGET_FRACTION == 0.7


def test_first_overflow_elides_to_the_target() -> None:
    tier = _tier()
    budget = budget_threshold(tier)
    messages = _tool_turn(["A" * 40_000, "B" * 40_000, "C" * 40_000])
    _fitted, stats = fit_inflight_turn(messages, tier, _COUNTER)
    assert stats is not None
    assert stats.tokens_before > budget
    assert stats.tokens_after <= _target(budget)
    # Largest-first absorbs the excess: it lands near the target, not at the floor.
    assert stats.tokens_after >= 0.95 * _target(budget)


def test_between_target_and_budget_nothing_changes() -> None:
    tier = _tier()
    budget = budget_threshold(tier)
    messages = _tool_turn(["D" * 4 * (_target(budget) + 1_000)])
    tokens, _ = estimate_tokens(messages, _COUNTER)
    assert _target(budget) < tokens <= budget
    fitted, stats = fit_inflight_turn(messages, tier, _COUNTER)
    assert stats is None
    assert fitted is messages


def test_small_new_result_does_not_rewrite_history() -> None:
    tier = _tier()
    first, stats = fit_inflight_turn(_tool_turn(["E" * 70_000, "F" * 30_000]), tier, _COUNTER)
    assert stats is not None
    grown = _with_small_result(first)
    second, again = fit_inflight_turn(grown, tier, _COUNTER)
    assert again is None
    assert second is grown
    assert second[: len(first)] == first


def test_without_the_margin_the_next_result_rewrites_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression TD-5010 fixes: landing at the budget re-elides."""
    monkeypatch.setattr(context_fit, "ELIDE_TARGET_FRACTION", 1.0)
    tier = _tier()
    first, stats = fit_inflight_turn(_tool_turn(["E" * 70_000, "F" * 30_000]), tier, _COUNTER)
    assert stats is not None
    second, again = fit_inflight_turn(_with_small_result(first), tier, _COUNTER)
    assert again is not None
    assert second[: len(first)] != first


def test_overflow_retry_budget_still_halves() -> None:
    tier = _tier()
    budget = budget_threshold(tier)
    messages = _tool_turn(["G" * 40_000, "H" * 40_000, "I" * 40_000, "J" * 40_000])
    fitted, full = fit_inflight_turn(messages, tier, _COUNTER)
    assert full is not None
    half = max(1, budget // 2)
    assert full.tokens_after > half
    _tighter, retry = fit_inflight_turn(fitted, tier, _COUNTER, budget=half)
    assert retry is not None
    assert retry.tokens_after <= _target(half)
    assert retry.tokens_after < full.tokens_after


def test_results_at_the_floor_stop_the_loop() -> None:
    """When every result is at the keep floor, elision ends above the target."""
    tier = _tier(context_window=4_000, max_output_tokens=500)
    budget = budget_threshold(tier)
    messages = _tool_turn(["K" * 2_000 for _ in range(10)])
    fitted, stats = fit_inflight_turn(messages, tier, _COUNTER)
    assert stats is not None
    assert stats.elided_results == 10
    assert stats.kept_messages == len(messages)
    assert _target(budget) < stats.tokens_after <= budget
    for message in fitted:
        if message.role == "tool":
            assert _ELISION_NOTE in content_as_text(message.content)


@pytest.mark.parametrize(
    ("floor", "expected"),
    [
        (1_000, 7_000),  # the usual 70%
        (4_000, 7_000),  # results still keep 30% of the budget above the floor
        (5_000, 8_000),  # the target rises with the floor
        (7_000, 10_000),  # at a 70% floor: the budget itself, as before TD-5010
        (12_000, 10_000),  # over the budget: never aim above it
    ],
)
def test_elision_target_rises_with_the_floor(floor: int, expected: int) -> None:
    assert elision_target(10_000, floor) == expected


def _old_call(call_id: str) -> ToolCall:
    return ToolCall(
        id=call_id, type="function", function=FunctionCall(name="fs_read", arguments="{}")
    )


def _second_turn(history_chars: int, bodies: list[str]) -> list[ChatMessage]:
    """Turn 1's read, kept verbatim by compaction, then turn 2's reads."""
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content="s" * 8_000),
        ChatMessage(role="user", content="read the parser"),
        ChatMessage(role="assistant", content=None, tool_calls=[_old_call("old")]),
        ChatMessage(role="tool", content="o" * (history_chars - 8_000), tool_call_id="old"),
        ChatMessage(role="assistant", content="Read it."),
    ]
    return [*messages, *_tool_turn(bodies, user="now the tests")[1:]]


def test_history_over_seventy_percent_cuts_only_the_excess() -> None:
    """EZER's shape: what elision cannot touch is already 80% of the budget."""
    tier = _tier(context_window=32768, max_output_tokens=2048)
    budget = budget_threshold(tier)
    bodies = ["a" * 9_000, "b" * 6_000, "c" * 5_000]
    messages = _second_turn(4 * int(budget * 0.8), bodies)
    history, _ = estimate_tokens(messages[:5], _COUNTER)
    assert history > _target(budget)

    fitted, stats = fit_inflight_turn(messages, tier, _COUNTER)

    assert stats is not None
    assert stats.tokens_before > budget
    # The old trim: only the largest result, only by the excess.
    assert _target(budget) < stats.tokens_after <= budget
    assert stats.elided_results == 1
    lengths = [len(content_as_text(m.content)) for m in fitted[-3:]]
    assert 1_000 < lengths[0] < 9_000
    assert lengths[1:] == [6_000, 5_000]
    assert fitted[:5] == messages[:5]


def test_largest_result_absorbs_a_calibrated_excess_in_one_cut() -> None:
    """Rounding under a learned ratio must not open a second result."""
    tier = _tier(context_window=32768, max_output_tokens=2048)
    target = _target(budget_threshold(tier))
    for ratio in (1.36, 1.55, 2.3):
        counter = _calibrated(ratio)
        # Many sizes, so the per-message rounding hits every remainder.
        for big in range(52_000, 52_400, 7):
            sizes = [12_000, big, 9_000, 3_000]
            fitted, stats = fit_inflight_turn(
                _tool_turn(["z" * size for size in sizes]), tier, counter
            )
            assert stats is not None
            after = [len(content_as_text(m.content)) for m in fitted if m.role == "tool"]
            assert stats.elided_results == 1, (ratio, sizes, after)
            assert after[0] == 12_000 and after[2:] == [9_000, 3_000]
            assert stats.tokens_after <= target


# ── Loop ────────────────────────────────────────────────────────────────


def _three_rounds(workspace: Path) -> MockProvider:
    (workspace / "small.md").write_text("s" * 2_000, encoding="utf-8")
    return MockProvider(
        sequences={
            "ezer-forge": [
                Script(kind="tool_call", tool_batch=_write_reads(workspace)),
                Script(
                    kind="tool_call",
                    tool_name="fs_read",
                    tool_arguments=json.dumps({"path": "small.md"}),
                ),
                Script(kind="stream", content="Here is the page."),
            ]
        }
    )


def _messages(request: ChatCompletionRequest) -> list[ChatMessage]:
    return list(request.messages)


def _elisions(session: Session) -> list[ContextCompacted]:
    return [e for e in session.event_log.all_events if isinstance(e, ContextCompacted)]


async def _drive(session: Session, mock: MockProvider, workspace: Path) -> None:
    runner = await _run(session, mock, _forge_config(), workspace)
    try:
        await session.add_user_message("Write an HTML page about the project")
        turn = await wait_for_turn(session, 1, 15.0)
    finally:
        await runner.cancel()
    assert turn.failed is False


async def test_the_next_round_keeps_the_sent_prefix(tmp_path: Path) -> None:
    mock = _three_rounds(tmp_path)
    session = Session(str(tmp_path))
    await _drive(session, mock, tmp_path)

    assert len(mock.calls) == 3
    budget = budget_threshold(_forge_config().tier("brain"))
    elided = _messages(mock.calls[1])
    tokens, _ = estimate_tokens(elided, _COUNTER, mock.calls[1].tools)
    assert tokens <= _target(budget)
    assert any(_ELISION_NOTE in content_as_text(m.content) for m in elided if m.role == "tool")
    assert _messages(mock.calls[2])[: len(elided)] == elided
    assert len(_elisions(session)) == 1


async def test_without_the_margin_the_loop_rewrites_the_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context_fit, "ELIDE_TARGET_FRACTION", 1.0)
    mock = _three_rounds(tmp_path)
    session = Session(str(tmp_path))
    await _drive(session, mock, tmp_path)

    assert len(mock.calls) == 3
    elided = _messages(mock.calls[1])
    assert _messages(mock.calls[2])[: len(elided)] != elided
    assert len(_elisions(session)) == 2


async def test_overflow_retry_still_recovers(tmp_path: Path) -> None:
    mock = MockProvider(
        sequences={
            "ezer-forge": [
                Script(kind="tool_call", tool_batch=_write_reads(tmp_path)),
                _overflow_script(),
                Script(kind="stream", content="Recovered."),
            ]
        }
    )
    session = Session(str(tmp_path))
    await _drive(session, mock, tmp_path)

    assert len(mock.calls) == 3
    half = max(1, budget_threshold(_forge_config().tier("brain")) // 2)
    first, _ = estimate_tokens(_messages(mock.calls[1]), _COUNTER, mock.calls[1].tools)
    retry, _ = estimate_tokens(_messages(mock.calls[2]), _COUNTER, mock.calls[2].tools)
    assert retry <= _target(half)
    assert retry < first
    assert "Recovered." in _assistant_text(session)
