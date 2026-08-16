"""``turn_complete`` reports the whole turn, not its last leg (TD-1806).

The loop reset the turn accumulator and the stopwatch inside its tool-call
round-trip loop, so a turn that called a tool reported only its final
provider call: TD-1804's live run measured ``tokens=1783`` against ledger
rows summing to 3520.  The ledger was right; the turn total was not.

The trap is that the fix sits next to a guarantee it must not disturb.
TD-1804 pins **one ledger row and one cost_update per provider call**; this
story is only about the **per-turn** accumulator.  So every test here
asserts both halves at once — the turn total is the sum, *and* the per-call
count is still two.

The harness is TD-1804's, deliberately: counting rows in the same audit
database the same way keeps the two stories' numbers comparable, so a
change to one story's measurement cannot quietly move the other's.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.test_tier_state import make_echo_session
from tests.test_usage_recording import (
    ScriptedProvider,
    TurnOutcome,
    _finish,
    _run_turn,
    _text,
    _tool_call_chunk,
    _usage_only,
)
from tstd.audit import AuditStore
from tstd.provider import ChatCompletionRequest, ProviderError, StreamChunk, Usage

# Two calls with deliberately different counts: a sum, a max and a
# last-wins are three different numbers here, so the assertion can only
# pass for one of them.
FIRST_CALL = Usage(prompt_tokens=1_200, cached_prompt_tokens=1_000, completion_tokens=480)
SECOND_CALL = Usage(prompt_tokens=2_000, cached_prompt_tokens=1_100, completion_tokens=140)

FIRST_CALL_TOKENS = 1_680
SECOND_CALL_TOKENS = 2_140
WHOLE_TURN_TOKENS = FIRST_CALL_TOKENS + SECOND_CALL_TOKENS

# The first leg is made to take real wall time.  Offline every leg runs in
# under a millisecond, where a stopwatch started at the wrong moment still
# reads "about zero" and no threshold can separate the two.
FIRST_LEG_DELAY = 0.25


class SlowFirstCallProvider(ScriptedProvider):
    """Scripted, with measurable wall time spent in the first call only."""

    async def chat_completion_stream(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[StreamChunk | ProviderError]:
        if not self.calls:
            await asyncio.sleep(FIRST_LEG_DELAY)
        async for chunk in super().chat_completion_stream(request):
            yield chunk


def _tool_round_trip_provider() -> SlowFirstCallProvider:
    """A turn shaped like the one that surfaced the bug: call, tool, call.

    Both legs use the split ordering (``finish_reason`` then ``usage``)
    that TD-1804 made recordable, so this exercises the real local-model
    stream shape rather than the co-emitting mock.
    """
    return SlowFirstCallProvider(
        [
            _tool_call_chunk("echo", '{"message": "hi"}'),
            _finish("tool_calls"),
            _usage_only(FIRST_CALL),
        ],
        [_text("done"), _finish("stop"), _usage_only(SECOND_CALL)],
    )


async def _two_call_turn(tmp_path: Path) -> tuple[SlowFirstCallProvider, TurnOutcome]:
    """Run one turn that spends two provider calls with a tool in between."""
    store = AuditStore(tmp_path / "audit.db")
    session, dispatcher = make_echo_session(tmp_path)
    provider = _tool_round_trip_provider()
    try:
        outcome = await _run_turn(provider, store, session=session, dispatcher=dispatcher)
    finally:
        store.close()
    return provider, outcome


# ── AC-1: the turn total is the sum across the turn's provider calls ───


class TestTurnTokens:
    async def test_turn_tokens_sum_every_call_in_the_turn(self, tmp_path: Path) -> None:
        """The reported symptom: a turn with a tool call billed one leg."""
        provider, outcome = await _two_call_turn(tmp_path)

        assert len(provider.calls) == 2
        assert outcome.turn.tokens == WHOLE_TURN_TOKENS
        # Named explicitly so a regression reads as what it is rather than
        # as an arbitrary number drifting.
        assert outcome.turn.tokens != SECOND_CALL_TOKENS  # the last leg only
        assert outcome.turn.tokens != FIRST_CALL_TOKENS  # the first leg only

    async def test_turn_cost_sums_every_call_in_the_turn(self, tmp_path: Path) -> None:
        _provider, outcome = await _two_call_turn(tmp_path)

        ledger_cost = sum(cost for _prompt, _completion, cost in outcome.ledger_rows)
        assert outcome.turn.cost == pytest.approx(ledger_cost, abs=1e-6)
        assert outcome.turn.cost > max(cost for _p, _c, cost in outcome.ledger_rows)


# ── AC-2: the duration covers the turn, not its final leg ──────────────


class TestTurnDuration:
    async def test_duration_covers_the_whole_turn(self, tmp_path: Path) -> None:
        """The first leg's wall time is inside the turn, so it must be
        inside the turn's duration.  A stopwatch restarted at the last
        round-trip cannot see it."""
        _provider, outcome = await _two_call_turn(tmp_path)

        assert outcome.turn.duration >= FIRST_LEG_DELAY


# ── AC-3: TD-1804's per-call guarantee is preserved, not traded away ───


class TestPerCallAccountingUnchanged:
    async def test_two_calls_still_write_two_rows_and_two_updates(self, tmp_path: Path) -> None:
        """Accumulating per turn must not collapse the per-call ledger:
        that would understate the session and blind the audit trail."""
        _provider, outcome = await _two_call_turn(tmp_path)

        assert len(outcome.ledger_rows) == 2
        assert len(outcome.cost_updates) == 2
        assert [(prompt, completion) for prompt, completion, _cost in outcome.ledger_rows] == [
            (FIRST_CALL.prompt_tokens, FIRST_CALL.completion_tokens),
            (SECOND_CALL.prompt_tokens, SECOND_CALL.completion_tokens),
        ]

    async def test_cost_update_turn_cost_accrues_across_the_turn(self, tmp_path: Path) -> None:
        """The entailed change, pinned deliberately: ``turn_cost`` on the
        meter is now the turn so far rather than the last call, and the
        final update agrees with ``turn_complete``."""
        _provider, outcome = await _two_call_turn(tmp_path)

        first, second = outcome.cost_updates
        assert second.turn_cost > first.turn_cost
        assert second.turn_cost == pytest.approx(outcome.turn.cost, abs=1e-6)
        # Per-call spend is still per call: session cost is the same sum.
        assert second.session_cost == pytest.approx(second.turn_cost, abs=1e-6)


# ── The opposite bug: an accumulator that never resets ─────────────────


class TestTurnBoundary:
    async def test_a_new_turn_starts_from_zero(self, tmp_path: Path) -> None:
        """Moving the reset out of the round-trip loop must not move it out
        of the turn loop as well — the second turn would then bill the
        first turn's tokens again."""
        store = AuditStore(tmp_path / "audit.db")
        provider = ScriptedProvider([_text("hi"), _finish("stop"), _usage_only(FIRST_CALL)])
        try:
            outcome = await _run_turn(provider, store, turns=2)
        finally:
            store.close()

        assert len(provider.calls) == 2
        assert len(outcome.ledger_rows) == 2  # both turns are on the ledger
        assert outcome.turn.tokens == FIRST_CALL_TOKENS  # but the turn is one call
