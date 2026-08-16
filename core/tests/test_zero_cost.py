"""Local preset and zero-cost accounting (TD-1802).

A free tier is still a tracked tier. These tests pin four things the code
already did by accident and now does on purpose:

1. the shipped ``local`` preset is entirely on loopback;
2. a price of ``0.0`` produces a finite ``0.0`` everywhere, never a
   ``ZeroDivisionError`` and never a ``NaN``;
3. the audit ledger records real token counts for a zero-price call —
   free is not untracked;
4. ``context_window`` and ``max_output_tokens`` come from config, so a
   slug the code has never seen carries its configured window.

Preset values are read through the real loader rather than duplicated
here (§2.7), so retargeting the preset is picked up without editing this
file.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from tests.test_loop import mock_factory, wait_for_turn
from tstd.audit import AuditStore
from tstd.audit_queries import cost_by_session
from tstd.audit_writer import AuditWriter
from tstd.compaction import budget_threshold
from tstd.config import (
    ModelConfig,
    Preset,
    TierConfig,
    TierName,
    default_config_yaml,
    is_loopback_url,
)
from tstd.cost import CostTracker, compute_call_cost, compute_call_details
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.provider import Usage
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner

# ── Helpers ────────────────────────────────────────────────────────────


def _shipped_local() -> ModelConfig:
    """The shipped config forced onto the ``local`` preset."""
    config = ModelConfig.model_validate(yaml.safe_load(default_config_yaml()))
    return config.model_copy(update={"active_preset": "local"})


def _free_tier(slug: str = "some-model-nobody-has-heard-of:1t") -> TierConfig:
    """A zero-price tier whose slug the code has never seen."""
    return TierConfig(
        slug=slug,
        base_url="http://127.0.0.1:11434/v1",
        input_price=0.0,
        output_price=0.0,
        cache_read_price=0.0,
        context_window=32_768,
        max_output_tokens=4_096,
    )


def _free_config() -> ModelConfig:
    """A three-tier config where every tier is free."""
    return ModelConfig(
        presets={
            "free": Preset(
                brain=_free_tier("free-brain"),
                worker=_free_tier("free-worker"),
                validator=_free_tier("free-validator"),
            )
        },
        active_preset="free",
    )


# Real traffic: a cached prefix, uncached remainder, and a completion.
_USAGE = Usage(prompt_tokens=1234, cached_prompt_tokens=200, completion_tokens=567)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[AuditStore]:
    s = AuditStore(tmp_path / "audit.db")
    yield s
    s.close()


# ── AC-1: the shipped preset ───────────────────────────────────────────


class TestLocalPresetShips:
    def test_local_preset_exists(self) -> None:
        assert "local" in _shipped_local().presets

    @pytest.mark.parametrize("tier", ["brain", "worker", "validator"])
    def test_every_tier_is_a_loopback_endpoint(self, tier: TierName) -> None:
        """Asserted as a property, not a literal: §2.7 keeps URLs out of
        source, and test source is source."""
        tier_cfg = _shipped_local().tiers()[tier]
        assert is_loopback_url(tier_cfg.base_url), tier_cfg.base_url

    @pytest.mark.parametrize("tier", ["brain", "worker", "validator"])
    def test_every_tier_names_a_port_and_a_scheme(self, tier: TierName) -> None:
        """A bare host would resolve as remote and reinstate the key
        requirement; a missing port would not reach any local server."""
        parts = urlsplit(_shipped_local().tiers()[tier].base_url)
        assert parts.scheme in ("http", "https")
        assert parts.port is not None

    def test_all_three_tiers_share_one_endpoint(self) -> None:
        """A single local server holds one loaded model; splitting tiers
        across endpoints would pay an unload/reload per routing hop."""
        urls = {t.base_url for t in _shipped_local().tiers().values()}
        assert len(urls) == 1

    @pytest.mark.parametrize("tier", ["brain", "worker", "validator"])
    def test_every_tier_is_free(self, tier: TierName) -> None:
        tier_cfg = _shipped_local().tiers()[tier]
        assert (tier_cfg.input_price, tier_cfg.output_price, tier_cfg.cache_read_price) == (
            0.0,
            0.0,
            0.0,
        )

    def test_local_preset_needs_no_api_key(self) -> None:
        """The point of an all-loopback preset (TD-1801)."""
        assert _shipped_local().requires_api_key() is False


# ── AC-2: zero prices flow through cost accounting ─────────────────────


class TestZeroPriceArithmetic:
    def test_every_reported_number_survives_a_zero_price(self) -> None:
        """The whole reporting surface, swept at price 0.0.

        A price used as a denominator anywhere raises ``ZeroDivisionError``
        the moment the price is zero, so exercising every accessor is a
        stronger audit than reading the arithmetic: it covers the paths a
        hand-picked fixture would miss. Every number must also be finite —
        an ``inf`` or ``nan`` reaching ``CostUpdate`` would render as a
        broken meter rather than a free one.
        """
        tracker = CostTracker(_free_config())
        tracker.begin_turn()
        tracker.record("brain", _USAGE)
        tracker.record_classifier("worker", _USAGE)

        reported: dict[str, float | int] = {
            **tracker.summary(),
            "turn_cached_tokens": tracker.turn_cached_tokens(),
            "turn_uncached_tokens": tracker.turn_uncached_tokens(),
            "last_cached_prompt_tokens": tracker.last_cached_prompt_tokens or 0,
            **tracker.cost_by_tier(),
        }
        for name, value in reported.items():
            assert math.isfinite(value), f"{name} is not finite: {value}"

    def test_compute_call_cost_is_finite_zero(self) -> None:
        cost = compute_call_cost(_USAGE, _free_tier())
        assert cost == 0.0
        assert math.isfinite(cost)

    def test_compute_call_details_keeps_real_token_counts(self) -> None:
        details = compute_call_details(_USAGE, _free_tier())
        assert details["uncached_prompt_tokens"] == 1034
        for key in ("prompt_cost", "cached_cost", "completion_cost", "total_cost"):
            assert details[key] == 0.0
            assert math.isfinite(details[key])

    def test_tracker_records_zero_cost_without_losing_tokens(self) -> None:
        tracker = CostTracker(_free_config())
        tracker.begin_turn()
        assert tracker.record("brain", _USAGE) == 0.0
        assert tracker.turn_tokens() == 1801
        assert tracker.session_tokens() == 1801
        assert tracker.turn_cost() == 0.0
        assert tracker.session_cost() == 0.0
        assert tracker.day_cost() == 0.0

    def test_cache_ratio_is_unaffected_by_price(self) -> None:
        """``turn_cache_ratio`` is the one variable-denominator division
        in cost.py; its denominator is a token count, not a price."""
        tracker = CostTracker(_free_config())
        tracker.begin_turn()
        tracker.record("brain", _USAGE)
        assert tracker.turn_cache_ratio() == pytest.approx(200 / 1234)

    def test_cache_ratio_with_no_tokens_does_not_divide_by_zero(self) -> None:
        tracker = CostTracker(_free_config())
        tracker.begin_turn()
        tracker.record("brain", Usage(prompt_tokens=0, completion_tokens=0))
        assert tracker.turn_cache_ratio() == 0.0

    def test_meter_reads_zero(self) -> None:
        """The cost_update the meter renders: exact zeros, not NaN."""
        tracker = CostTracker(_free_config())
        tracker.begin_turn()
        tracker.record("brain", _USAGE)
        tracker.record_classifier("worker", _USAGE)
        update = tracker.emit_cost_update(session_id="s1")
        for value in (
            update.turn_cost,
            update.session_cost,
            update.total_cost,
            update.classifier_cost,
            *update.cost_by_tier.values(),
        ):
            assert value == 0.0
            assert math.isfinite(value)

    def test_a_free_tier_still_appears_in_the_breakdown(self) -> None:
        """Zero spend is a fact worth showing, not an absence."""
        tracker = CostTracker(_free_config())
        tracker.begin_turn()
        tracker.record("brain", _USAGE)
        assert tracker.cost_by_tier() == {"brain": 0.0}


# ── AC-3: free is not untracked ────────────────────────────────────────


class TestLedgerRecordsFreeCalls:
    async def test_zero_price_turn_writes_real_token_counts(self, store: AuditStore) -> None:
        """A full loop turn on an all-free preset lands in the ledger with
        the tokens intact and the cost honestly zero."""
        session = Session("/tmp/free-ws")
        writer = AuditWriter(store)
        writer.start()
        writer.attach_session(session)

        mock = MockProvider(scripts={"free-brain": Script(kind="stream", content="Hi")})
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

        turn_id, tokens, cost = store._conn.execute("SELECT id, tokens, cost FROM turns").fetchone()
        assert tokens > 0, "a free turn still consumed tokens"
        assert cost == 0.0

        calls = store._conn.execute(
            "SELECT turn_id, prompt_tokens, completion_tokens, cost FROM model_calls"
        ).fetchall()
        assert calls, "a free call must still produce a ledger row"
        for row_turn_id, prompt_tokens, completion_tokens, call_cost in calls:
            assert row_turn_id == turn_id
            assert prompt_tokens > 0
            assert completion_tokens > 0
            assert call_cost == 0.0

    async def test_cost_aggregate_reports_free_tokens(self, store: AuditStore) -> None:
        """The aggregation the ledger view reads: tokens present, spend
        zero. A session that vanishes from this query is untracked."""
        session = Session("/tmp/free-ws")
        writer = AuditWriter(store)
        writer.start()
        writer.attach_session(session)

        mock = MockProvider(scripts={"free-brain": Script(kind="stream", content="Hi")})
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

        aggregates = [a for a in cost_by_session(store) if a.key == session.id]
        assert len(aggregates) == 1
        aggregate = aggregates[0]
        assert aggregate.prompt_tokens > 0
        assert aggregate.completion_tokens > 0
        assert aggregate.cost == 0.0


# ── AC-4: window and output cap come from config ───────────────────────


class TestWindowComesFromConfig:
    def test_unknown_slug_keeps_its_configured_window(self) -> None:
        """No slug→window table exists; an unrecognised model is not
        silently given a default window."""
        tier = _free_tier()
        assert tier.context_window == 32_768
        assert budget_threshold(tier) == int((32_768 - 4_096) * 0.8)

    def test_same_slug_different_window_gives_different_budget(self) -> None:
        """The proof that the slug is not consulted: hold it fixed and the
        budget still tracks config."""
        small = _free_tier().model_copy(update={"context_window": 8_192})
        large = _free_tier().model_copy(update={"context_window": 262_144})
        assert small.slug == large.slug
        assert budget_threshold(small) < budget_threshold(large)
        assert budget_threshold(small) == int((8_192 - 4_096) * 0.8)
        assert budget_threshold(large) == int((262_144 - 4_096) * 0.8)

    def test_max_output_tokens_is_reserved_from_the_window(self) -> None:
        """Both numbers come from config, and the output cap is subtracted
        rather than assumed."""
        generous = _free_tier().model_copy(update={"max_output_tokens": 16_384})
        assert budget_threshold(generous) == int((32_768 - 16_384) * 0.8)

    @pytest.mark.parametrize("tier", ["brain", "worker", "validator"])
    def test_shipped_local_tiers_declare_both_numbers(self, tier: TierName) -> None:
        tier_cfg = _shipped_local().tiers()[tier]
        assert tier_cfg.context_window > tier_cfg.max_output_tokens
        assert budget_threshold(tier_cfg) > 0
