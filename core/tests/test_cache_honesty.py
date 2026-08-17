"""Cache telemetry reports reuse the provider claimed (TD-1811).

TD-305 orders the prompt so a provider *can* cache the prefix. Whether one
did is a separate fact, and the only witness to it is the provider's own
``usage`` object. Measured 2026-08-17 against `qwen3.8:27b` on Ollama at
``127.0.0.1:11434``: the response carries exactly ``prompt_tokens``,
``completion_tokens`` and ``total_tokens`` — no ``prompt_tokens_details``,
no ``cached_tokens``, on the streaming path or the blocking one. llama.cpp
builds context checkpoints and then erases them ("cached n_tokens = 0"), so
an identical prefix is re-prefilled every turn.

These tests pin three separations the code now makes:

1. **absent is not zero.** A provider that says nothing about cache leaves
   the figure ``None``. Folding it to ``0`` would have the panel report a
   *miss* — a claim about the provider's cache that the provider never
   made.
2. **unknown is never a number.** No surface converts silence into a
   token count, a ratio or a discount. The cost path is the one exception,
   and it errs toward charging full price (:func:`billable_cached_tokens`).
3. **zero is not blank, and never stale.** A turn with no reported reuse
   reports ``0.0``, not an empty field and not the previous turn's rate.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_dispatch import make_config, mock_factory, start_loop, wait_for_turn
from tstd.audit import AuditStore
from tstd.audit_writer import AuditWriter
from tstd.config import ModelConfig, Preset, TierConfig
from tstd.context import ContextAssembler, SteeringFileResolver
from tstd.context.stack import build_instruction_stack
from tstd.cost import CostTracker, billable_cached_tokens, compute_call_cost
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.protocol import InstructionStack
from tstd.provider import Usage
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner

# The usage object Ollama actually returned on 2026-08-17, verbatim.
OLLAMA_USAGE = {"prompt_tokens": 1955, "completion_tokens": 8, "total_tokens": 1963}

# What an OpenAI-compatible provider that *does* report cache sends.
REPORTING_USAGE = {
    "prompt_tokens": 1955,
    "completion_tokens": 8,
    "total_tokens": 1963,
    "prompt_tokens_details": {"cached_tokens": 0},
}


def _free_config() -> ModelConfig:
    """An all-loopback, all-zero-price preset (the TD-1802 `local` shape)."""

    def tier(slug: str) -> TierConfig:
        return TierConfig(
            slug=slug,
            base_url="http://127.0.0.1:11434/v1",
            input_price=0.0,
            output_price=0.0,
            cache_read_price=0.0,
            context_window=32_768,
            max_output_tokens=4_096,
        )

    return ModelConfig(
        presets={
            "free": Preset(
                brain=tier("free-brain"),
                worker=tier("free-worker"),
                validator=tier("free-validator"),
            )
        },
        active_preset="free",
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[AuditStore]:
    s = AuditStore(tmp_path / "audit.db")
    yield s
    s.close()


async def _turn_complete_logs(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    script: Script,
    turns: int = 2,
) -> list[dict[str, object]]:
    """Run *turns* real loop turns over one unchanging workspace and return
    the ``extra_fields`` of each "turn complete" log line.

    The workspace steering is written once and never touched, so every turn
    assembles a byte-identical prefix — the condition the story is about.
    """
    (tmp_path / "AGENTS.md").write_text("Stay in the workspace. Use absolute paths.\n")
    session = Session(str(tmp_path))
    mock = MockProvider(default=script)

    with caplog.at_level(logging.INFO, logger="tstd.loop"):
        runner = await start_loop(session, TierRouter(lead_turns=99), mock, make_config())
        for n in range(1, turns + 1):
            await session.add_user_message(f"message {n}")
            await wait_for_turn(session, n)
        await runner.cancel()

    return [
        dict(r.extra_fields)
        for r in caplog.records
        if r.name == "tstd.loop" and r.getMessage() == "turn complete"
    ]


# ── The provider signal: absent is not zero ─────────────────────────────


class TestProviderSignal:
    def test_measured_ollama_response_reports_no_cache_figure(self) -> None:
        """The live shape: three keys, none of them about cache."""
        usage = Usage.from_api_dict(OLLAMA_USAGE)
        assert usage.prompt_tokens == 1955
        assert usage.cached_prompt_tokens is None

    def test_reported_zero_is_a_reported_zero(self) -> None:
        """An explicit ``cached_tokens: 0`` is a fact and survives as one."""
        assert Usage.from_api_dict(REPORTING_USAGE).cached_prompt_tokens == 0

    def test_reported_reuse_passes_through(self) -> None:
        data = dict(REPORTING_USAGE, prompt_tokens_details={"cached_tokens": 1200})
        assert Usage.from_api_dict(data).cached_prompt_tokens == 1200

    def test_no_usage_at_all_reports_nothing(self) -> None:
        assert Usage.from_api_dict(None).cached_prompt_tokens is None

    def test_malformed_details_reports_nothing(self) -> None:
        """A non-dict ``prompt_tokens_details`` is unreadable, not zero."""
        data = dict(OLLAMA_USAGE, prompt_tokens_details="unexpected")
        assert Usage.from_api_dict(data).cached_prompt_tokens is None


# ── The tracker invents nothing ─────────────────────────────────────────


class TestTrackerNeverInvents:
    def test_silence_leaves_the_figure_unknown_after_a_call(self) -> None:
        """``None`` after a call that reported nothing — and ``cache_observed``
        says a call happened, so the viewer can tell this from "no turn yet"."""
        config = make_config()
        tracker = CostTracker(config)
        assert tracker.cache_observed is False

        tracker.begin_turn()
        tracker.record("brain", Usage.from_api_dict(OLLAMA_USAGE), config.tier("brain"))

        assert tracker.cache_observed is True
        assert tracker.last_cached_prompt_tokens is None

    def test_a_reported_miss_is_zero_not_unknown(self) -> None:
        config = make_config()
        tracker = CostTracker(config)
        tracker.begin_turn()
        tracker.record("brain", Usage.from_api_dict(REPORTING_USAGE), config.tier("brain"))

        assert tracker.cache_observed is True
        assert tracker.last_cached_prompt_tokens == 0

    def test_ratio_is_zero_when_the_provider_reported_nothing(self) -> None:
        config = make_config()
        tracker = CostTracker(config)
        tracker.begin_turn()
        tracker.record("brain", Usage.from_api_dict(OLLAMA_USAGE), config.tier("brain"))

        assert tracker.turn_cache_ratio() == 0.0
        assert tracker.turn_cached_tokens() == 0

    def test_ratio_never_carries_the_previous_turn(self) -> None:
        """A cached turn followed by an uncached one reports 0.0, not 0.8."""
        config = make_config()
        tracker = CostTracker(config)

        tracker.begin_turn()
        tracker.record(
            "brain",
            Usage(prompt_tokens=1_000, completion_tokens=10, cached_prompt_tokens=800),
            config.tier("brain"),
        )
        assert tracker.turn_cache_ratio() == pytest.approx(0.8)

        tracker.begin_turn()
        tracker.record("brain", Usage.from_api_dict(OLLAMA_USAGE), config.tier("brain"))
        assert tracker.turn_cache_ratio() == 0.0

    def test_ratio_is_zero_not_blank_when_a_turn_spent_nothing(self) -> None:
        tracker = CostTracker(make_config())
        tracker.begin_turn()
        assert tracker.turn_cache_ratio() == 0.0


# ── AC-4: two turns, identical prefix, zero cached ──────────────────────


class TestIdenticalPrefixStaysZero:
    async def test_reported_zero_keeps_the_ratio_at_zero_across_two_turns(
        self, caplog: pytest.LogCaptureFixture, tmp_path: Path
    ) -> None:
        """The named criterion. A provider that reports ``cached_tokens: 0``
        on both turns of an unchanged prefix must be reported as zero reuse
        twice — the second turn does not inherit optimism from the first."""
        logs = await _turn_complete_logs(
            caplog, tmp_path, Script(kind="stream", content="ok", cached_tokens=0)
        )

        assert len(logs) == 2
        assert logs[0]["cache_prefix_hash"] == logs[1]["cache_prefix_hash"], (
            "the two turns must share a prefix for this to test anything"
        )
        assert [entry["cache_ratio"] for entry in logs] == [0.0, 0.0]
        # The provider spoke, so the zero is a report rather than a guess.
        assert [entry["cache_reported"] for entry in logs] == [True, True]

    async def test_a_silent_provider_reports_zero_and_says_it_was_silent(
        self, caplog: pytest.LogCaptureFixture, tmp_path: Path
    ) -> None:
        """The Ollama case. The ratio is still 0.0 — there is no saving to
        claim — but the log marks it unreported, so a 0.0 that means "the
        cache missed" is never mistaken for one that means "we were not
        told"."""
        logs = await _turn_complete_logs(
            caplog, tmp_path, Script(kind="stream", content="ok", cached_tokens=None)
        )

        assert len(logs) == 2
        assert logs[0]["cache_prefix_hash"] == logs[1]["cache_prefix_hash"]
        assert [entry["cache_ratio"] for entry in logs] == [0.0, 0.0]
        assert [entry["cache_reported"] for entry in logs] == [False, False]


# ── AC-3: free is tracked ───────────────────────────────────────────────


class TestFreeIsStillTracked:
    async def test_zero_price_silent_provider_records_real_prompt_tokens(
        self, store: AuditStore, tmp_path: Path
    ) -> None:
        """The local path: no price, no cache report — and still a full
        prompt-token count in the ledger. The same rule TD-1802 set for
        output tokens holds for prompt tokens on an unreported cache."""
        session = Session(str(tmp_path))
        writer = AuditWriter(store)
        writer.start()
        writer.attach_session(session)

        mock = MockProvider(
            scripts={
                "free-brain": Script(
                    kind="stream", content="Hi", prompt_tokens=1955, cached_tokens=None
                )
            }
        )
        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(
                s, TierRouter(), mock_factory(mock), _free_config(), audit_sink=writer
            ),
        )
        await runner.start()
        await session.add_user_message("hello")
        await wait_for_turn(session, 1)
        await writer._queue.join()
        await runner.cancel()

        rows = store._conn.execute(
            "SELECT prompt_tokens, cached_prompt_tokens, cost FROM model_calls"
        ).fetchall()
        assert rows, "a free call must still produce a ledger row"
        for prompt_tokens, cached, cost in rows:
            assert prompt_tokens == 1955, "free is not untracked"
            # The column is a ledger of *reported* reuse; a silent provider
            # contributes none, so sums over it stay sums of real claims.
            assert cached == 0
            assert cost == 0.0


# ── Cost never grants an unreported discount ────────────────────────────


class TestCostOfSilence:
    def test_unreported_cache_bills_the_whole_prompt_at_input_price(self) -> None:
        """Charging the cache rate on an assumed hit would understate spend
        on exactly the providers that never confirm one."""
        tier_cfg = make_config().tier("brain")  # input 1.0, cache_read 0.5 per M
        silent = Usage(prompt_tokens=1_000_000, completion_tokens=0)
        assert compute_call_cost(silent, tier_cfg) == pytest.approx(1.0)

    def test_a_reported_hit_is_priced_at_the_cache_rate(self) -> None:
        tier_cfg = make_config().tier("brain")
        hit = Usage(prompt_tokens=1_000_000, completion_tokens=0, cached_prompt_tokens=1_000_000)
        assert compute_call_cost(hit, tier_cfg) == pytest.approx(0.5)

    def test_the_pricing_fallback_is_explicit_about_both_inputs(self) -> None:
        assert billable_cached_tokens(None) == 0
        assert billable_cached_tokens(0) == 0
        assert billable_cached_tokens(1_200) == 1_200


# ── The surfaced figure keeps the distinction ───────────────────────────


class TestSurfacedStackFigure:
    def _event(self, tmp_path: Path, tracker: CostTracker) -> InstructionStack:
        """The panel's payload, built the way the daemon builds it."""
        (tmp_path / "AGENTS.md").write_text("rules\n")
        assembler = ContextAssembler(resolver=SteeringFileResolver(home_dir=tmp_path / "home"))
        steering = assembler.assemble_sync(tmp_path)
        return build_instruction_stack(
            "s1",
            steering,
            last_cached_tokens=tracker.last_cached_prompt_tokens,
            cache_observed=tracker.cache_observed,
        )

    def test_before_any_turn_the_event_says_nothing_was_observed(self, tmp_path: Path) -> None:
        event = self._event(tmp_path, CostTracker(make_config()))
        assert event.cache_observed is False
        assert event.last_cached_tokens is None

    def test_a_silent_provider_is_observed_but_unreported(self, tmp_path: Path) -> None:
        """The two states the old single nullable field could not tell
        apart: this one must not render as a miss."""
        config = make_config()
        tracker = CostTracker(config)
        tracker.begin_turn()
        tracker.record("brain", Usage.from_api_dict(OLLAMA_USAGE), config.tier("brain"))

        event = self._event(tmp_path, tracker)
        assert event.cache_observed is True
        assert event.last_cached_tokens is None

    def test_a_reported_miss_is_observed_and_zero(self, tmp_path: Path) -> None:
        config = make_config()
        tracker = CostTracker(config)
        tracker.begin_turn()
        tracker.record("brain", Usage.from_api_dict(REPORTING_USAGE), config.tier("brain"))

        event = self._event(tmp_path, tracker)
        assert event.cache_observed is True
        assert event.last_cached_tokens == 0
