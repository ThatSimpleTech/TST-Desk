"""Usage is recorded independently of ``finish_reason`` (TD-1804).

The loop used to record a call's usage only when one chunk carried both
``finish_reason`` and ``usage``.  Ollama 0.32.13 sends them on separate
chunks, so a local turn wrote no ledger row and moved no meter; OpenAI and
vLLM send a trailing usage-only chunk and lose the row the same way.

The fix cannot simply be "record on any chunk carrying usage" — a provider
that repeats cumulative usage would then be billed once per chunk.  What
these tests pin is therefore the invariant, not the branch: **one ledger
row and one ``cost_update`` per provider call**, whatever shape the stream
arrives in.

``MockProvider`` co-emits both fields on one chunk and must keep doing so
(TD-1401), so the orderings this story is about cannot be expressed with
it.  :class:`ScriptedProvider` replays chunk sequences verbatim, which is
what makes the split ordering a CI-catchable regression with no model
running.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.test_loop import make_config, wait_for_turn
from tests.test_tier_state import make_echo_session
from tstd.audit import AuditStore
from tstd.audit_writer import AuditWriter
from tstd.loop import ProviderLike, agent_loop
from tstd.mock import MockProvider, Script
from tstd.protocol import CostUpdate, TurnComplete
from tstd.provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Delta,
    DeltaToolCall,
    ProviderError,
    StreamChunk,
    Usage,
)
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.tools import ToolDispatcher

# ── A provider that replays exact chunk sequences ──────────────────────


class ScriptedProvider:
    """Replays one verbatim chunk sequence per call, in order.

    Deliberately dumber than ``MockProvider``: it scripts *chunks* rather
    than responses, because the thing under test is where a field rides on
    the wire.  The last sequence repeats if the loop calls more times than
    were scripted.
    """

    def __init__(self, *calls: Sequence[StreamChunk]) -> None:
        self._calls = [list(chunks) for chunks in calls]
        self.calls: list[ChatCompletionRequest] = []

    async def chat_completion_stream(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[StreamChunk | ProviderError]:
        chunks = self._calls[min(len(self.calls), len(self._calls) - 1)]
        self.calls.append(request)
        for chunk in chunks:
            yield chunk

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse | ProviderError:
        raise AssertionError("the scripted provider is streaming-only")


# Real traffic: a cached prefix, an uncached remainder, and a completion.
USAGE = Usage(
    prompt_tokens=1_200,
    cached_prompt_tokens=1_000,
    completion_tokens=480,
    total_tokens=1_680,
)


def _text(content: str) -> StreamChunk:
    return StreamChunk(id="c1", delta=Delta(content=content))


def _finish(reason: str = "stop") -> StreamChunk:
    """A chunk that closes the choice and carries no usage."""
    return StreamChunk(id="c1", delta=Delta(), finish_reason=reason)


def _usage_only(usage: Usage = USAGE) -> StreamChunk:
    """The trailing usage chunk: ``{"choices": [], "usage": {...}}``."""
    return StreamChunk(id="c1", delta=Delta(), finish_reason=None, usage=usage)


def _co_emit(usage: Usage = USAGE) -> StreamChunk:
    """One chunk carrying both fields — what ``MockProvider`` sends."""
    return StreamChunk(id="c1", delta=Delta(), finish_reason="stop", usage=usage)


def _tool_call_chunk(name: str, arguments: str) -> StreamChunk:
    return StreamChunk(
        id="c1",
        delta=Delta(
            tool_calls=[
                DeltaToolCall(
                    index=0, id="call-1", function_name=name, function_arguments=arguments
                )
            ]
        ),
    )


# The ordering measured against Ollama 0.32.13 with
# ``stream_options: {"include_usage": true}``: finish_reason lands first,
# alone, and usage arrives on the chunk after it.
SPLIT_STREAM: list[StreamChunk] = [_text("hello"), _finish("length"), _usage_only()]


# ── Harness ────────────────────────────────────────────────────────────


@dataclass
class TurnOutcome:
    """What one turn left behind on the wire and in the ledger."""

    turn: TurnComplete
    cost_updates: list[CostUpdate]
    ledger_rows: list[tuple[int, int, float]]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[AuditStore]:
    s = AuditStore(tmp_path / "audit.db")
    yield s
    s.close()


def _factory(provider: ProviderLike) -> Callable[[], Awaitable[ProviderLike]]:
    async def _make() -> ProviderLike:
        return provider

    return _make


async def _run_turn(
    provider: ProviderLike,
    store: AuditStore,
    *,
    session: Session | None = None,
    dispatcher: ToolDispatcher | None = None,
    turns: int = 1,
) -> TurnOutcome:
    """Drive *turns* real turns through the loop and read the aftermath.

    The audit database is the ledger the harness's ``ledger`` check reads,
    so counting ``model_calls`` rows here is the same measurement, not a
    proxy for it.
    """
    session = session or Session("/tmp/usage-ws")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(session)

    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            TierRouter(),
            _factory(provider),
            make_config(),
            tool_dispatcher=dispatcher,
            audit_sink=writer,
        ),
    )
    await runner.start()
    for n in range(turns):
        await session.add_user_message(f"turn {n}")
        turn = await wait_for_turn(session, n + 1)
    await writer._queue.join()
    await runner.cancel()

    rows = store._conn.execute(
        "SELECT prompt_tokens, completion_tokens, cost FROM model_calls"
    ).fetchall()
    updates = [e for e in session.event_log.all_events if isinstance(e, CostUpdate)]
    return TurnOutcome(turn=turn, cost_updates=updates, ledger_rows=list(rows))


# ── AC-1/2: a split stream is recorded, exactly once ───────────────────


class TestSplitOrdering:
    """The Ollama shape: ``finish_reason`` and ``usage`` on separate chunks."""

    async def test_split_stream_writes_one_ledger_row(self, store: AuditStore) -> None:
        outcome = await _run_turn(ScriptedProvider(SPLIT_STREAM), store)
        assert len(outcome.ledger_rows) == 1
        prompt_tokens, completion_tokens, cost = outcome.ledger_rows[0]
        assert (prompt_tokens, completion_tokens) == (1_200, 480)
        assert cost > 0

    async def test_split_stream_emits_one_cost_update(self, store: AuditStore) -> None:
        outcome = await _run_turn(ScriptedProvider(SPLIT_STREAM), store)
        assert len(outcome.cost_updates) == 1
        assert outcome.cost_updates[0].session_cost > 0

    async def test_split_stream_moves_the_meter(self, store: AuditStore) -> None:
        """The user-visible symptom: the turn reported 0 tokens and $0."""
        outcome = await _run_turn(ScriptedProvider(SPLIT_STREAM), store)
        assert outcome.turn.tokens == 1_680
        assert outcome.turn.cost > 0

    async def test_cost_update_precedes_turn_complete(self, store: AuditStore) -> None:
        """TD-1006: the meter moves as cost accrues, not at turn end."""
        outcome = await _run_turn(ScriptedProvider(SPLIT_STREAM), store)
        assert outcome.cost_updates[0].seq < outcome.turn.seq


# ── AC-1: any placement of usage is recorded, and only once ────────────


class TestUsagePlacement:
    @pytest.mark.parametrize(
        ("name", "chunks"),
        [
            ("split: finish then usage", SPLIT_STREAM),
            ("co-emitted on one chunk", [_text("hello"), _co_emit()]),
            ("usage before finish_reason", [_text("hello"), _usage_only(), _finish()]),
            ("usage with no finish_reason at all", [_text("hello"), _usage_only()]),
            ("usage on the very first chunk", [_usage_only(), _text("hello"), _finish()]),
        ],
    )
    async def test_one_row_and_one_update_whatever_the_shape(
        self, store: AuditStore, name: str, chunks: list[StreamChunk]
    ) -> None:
        outcome = await _run_turn(ScriptedProvider(chunks), store)
        assert len(outcome.ledger_rows) == 1, name
        assert len(outcome.cost_updates) == 1, name
        assert outcome.turn.tokens == 1_680, name

    async def test_a_stream_carrying_no_usage_records_nothing(self, store: AuditStore) -> None:
        """Absent usage is not invented — the ledger stays honest."""
        outcome = await _run_turn(ScriptedProvider([_text("hello"), _finish()]), store)
        assert outcome.ledger_rows == []
        assert outcome.cost_updates == []
        assert outcome.turn.tokens == 0


# ── The hazard: repeated usage must not double count ───────────────────


class TestRepeatedUsage:
    """A provider asked for continuous usage stats repeats a cumulative
    figure on every chunk.  Recording per usage-bearing chunk would bill it
    once per chunk; taking the first would bill a partial count."""

    async def test_repeated_cumulative_usage_records_once_with_final_counts(
        self, store: AuditStore
    ) -> None:
        partial = Usage(prompt_tokens=1_200, cached_prompt_tokens=1_000, completion_tokens=1)
        halfway = Usage(prompt_tokens=1_200, cached_prompt_tokens=1_000, completion_tokens=240)
        provider = ScriptedProvider(
            [
                StreamChunk(id="c1", delta=Delta(content="hel"), usage=partial),
                StreamChunk(id="c1", delta=Delta(content="lo"), usage=halfway),
                _finish(),
                _usage_only(),
            ]
        )
        outcome = await _run_turn(provider, store)

        assert len(outcome.ledger_rows) == 1
        assert len(outcome.cost_updates) == 1
        # Last-wins: the complete figure, never the first partial one.
        assert outcome.ledger_rows[0][:2] == (1_200, 480)


# ── AC-2/3: one row per *call*, not per turn and not once per session ──


class TestOncePerCall:
    async def test_two_calls_in_one_turn_record_twice(self, tmp_path: Path) -> None:
        """A tool round-trip is two provider calls, so two rows and two
        updates.  A record-once latch scoped to the turn would collapse
        them into one and understate the session."""
        store = AuditStore(tmp_path / "audit.db")
        session, dispatcher = make_echo_session(tmp_path)
        provider = ScriptedProvider(
            [_tool_call_chunk("echo", '{"message": "hi"}'), _finish("tool_calls"), _usage_only()],
            SPLIT_STREAM,
        )
        try:
            outcome = await _run_turn(provider, store, session=session, dispatcher=dispatcher)
        finally:
            store.close()

        assert len(provider.calls) == 2
        assert len(outcome.ledger_rows) == 2
        assert len(outcome.cost_updates) == 2
        # Session spend is the running total; it doubles between the two
        # updates.  ``turn_complete.tokens`` is not checked here because the
        # loop calls ``begin_turn`` once per provider call, so the turn
        # accumulator already reports the last call only — pre-existing
        # behaviour this story neither relies on nor changes.
        first, second = outcome.cost_updates
        assert second.session_cost == pytest.approx(2 * first.session_cost)

    async def test_a_second_turn_records_again(self, store: AuditStore) -> None:
        """A latch that never resets would silence every turn after the
        first — the same bug wearing the opposite sign."""
        outcome = await _run_turn(ScriptedProvider(SPLIT_STREAM), store, turns=2)
        assert len(outcome.ledger_rows) == 2
        assert len(outcome.cost_updates) == 2
        # turn_complete is per turn, so the second turn bills one call.
        assert outcome.turn.tokens == 1_680


# ── AC-3: MockProvider and TD-1401 are untouched ───────────────────────


class TestMockProviderUnchanged:
    async def test_mock_still_co_emits_finish_reason_and_usage(self) -> None:
        """The property TD-1401's harness depends on, asserted directly so
        a change to the mock fails here rather than somewhere downstream."""
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="Hi there")})
        chunks = [
            chunk
            async for chunk in mock.chat_completion_stream(
                ChatCompletionRequest(model="test-brain", messages=[], stream=True)
            )
            if isinstance(chunk, StreamChunk)
        ]
        carrying_usage = [c for c in chunks if c.usage is not None]
        assert len(carrying_usage) == 1
        assert carrying_usage[0].finish_reason == "stop"

    async def test_a_mock_turn_still_records_exactly_once(self, store: AuditStore) -> None:
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="Hi there")})
        outcome = await _run_turn(mock, store)
        assert len(outcome.ledger_rows) == 1
        assert len(outcome.cost_updates) == 1
        assert outcome.turn.tokens > 0
        assert outcome.turn.cost > 0
