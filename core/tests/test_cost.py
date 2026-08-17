"""Tests for cost and token accounting (TD-304).

Includes hand-computed expected dollar figures for each tier, including
a mixed cached/uncached case.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest import approx

from tstd.config import TierConfig, default_config_yaml, load_config
from tstd.cost import CallRecord, CostTracker, compute_call_cost, compute_call_details
from tstd.provider import Usage

# ── Hand-computed prices (from config.yaml tst-default preset) ──────────
# Brain:   $2.80/M in, $14.00/M out, $0.30/M cache-read
# Worker:  $0.07/M in,  $0.17/M out, $0.07/M cache-read
# Validator: $0.44/M in, $0.87/M out, $0.44/M cache-read

BRAIN = TierConfig(
    slug="moonshotai/kimi-k3",
    base_url="https://openrouter.ai/api/v1",
    input_price=2.80,
    output_price=14.00,
    cache_read_price=0.30,
    context_window=1_000_000,
    max_output_tokens=8192,
)

WORKER = TierConfig(
    slug="deepseek/deepseek-v4-flash",
    base_url="https://openrouter.ai/api/v1",
    input_price=0.07,
    output_price=0.17,
    cache_read_price=0.07,
    context_window=128_000,
    max_output_tokens=16384,
)

VALIDATOR = TierConfig(
    slug="deepseek/deepseek-v4-pro",
    base_url="https://openrouter.ai/api/v1",
    input_price=0.44,
    output_price=0.87,
    cache_read_price=0.44,
    context_window=128_000,
    max_output_tokens=8192,
)


# ── Helper ────────────────────────────────────────────────────────────────


def _usage(prompt: int, cached: int, completion: int) -> Usage:
    """Build a Usage with the given token counts."""
    return Usage(
        prompt_tokens=prompt,
        cached_prompt_tokens=cached,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )


# ── Cost computation ──────────────────────────────────────────────────────


class TestComputeCost:
    """Hand-computed expected dollar figures for each tier."""

    # ── Brain tier ──

    def test_brain_uncached(self) -> None:
        """Brain: 1000 prompt + 500 completion, no cache.
        cost = 1000 * 2.80/1e6 + 500 * 14.00/1e6
             = 0.002800 + 0.007000 = 0.009800
        """
        u = _usage(prompt=1000, cached=0, completion=500)
        cost = compute_call_cost(u, BRAIN)
        assert cost == approx(0.009800, rel=1e-6)

    def test_brain_fully_cached(self) -> None:
        """Brain: 1000 prompt (all cached) + 500 completion.
        uncached = 0
        cost = 0 * 2.80/1e6 + 1000 * 0.30/1e6 + 500 * 14.00/1e6
             = 0 + 0.000300 + 0.007000 = 0.007300
        """
        u = _usage(prompt=1000, cached=1000, completion=500)
        cost = compute_call_cost(u, BRAIN)
        assert cost == approx(0.007300, rel=1e-6)

    def test_brain_mixed(self) -> None:
        """Brain: 2000 prompt (500 cached) + 800 completion.
        uncached = 1500
        cost = 1500 * 2.80/1e6 + 500 * 0.30/1e6 + 800 * 14.00/1e6
             = 0.004200 + 0.000150 + 0.011200 = 0.015550
        """
        u = _usage(prompt=2000, cached=500, completion=800)
        cost = compute_call_cost(u, BRAIN)
        assert cost == approx(0.015550, rel=1e-6)

    # ── Worker tier ──

    def test_worker_uncached(self) -> None:
        """Worker: 5000 prompt + 2000 completion, no cache.
        cost = 5000 * 0.07/1e6 + 2000 * 0.17/1e6
             = 0.000350 + 0.000340 = 0.000690
        """
        u = _usage(prompt=5000, cached=0, completion=2000)
        cost = compute_call_cost(u, WORKER)
        assert cost == approx(0.000690, rel=1e-6)

    def test_worker_mixed(self) -> None:
        """Worker: 10000 prompt (5000 cached) + 4000 completion.
        uncached = 5000
        cost = 5000 * 0.07/1e6 + 5000 * 0.07/1e6 + 4000 * 0.17/1e6
             = 0.000350 + 0.000350 + 0.000680 = 0.001380
        """
        u = _usage(prompt=10000, cached=5000, completion=4000)
        cost = compute_call_cost(u, WORKER)
        assert cost == approx(0.001380, rel=1e-6)

    # ── Validator tier ──

    def test_validator_uncached(self) -> None:
        """Validator: 3000 prompt + 1000 completion, no cache.
        cost = 3000 * 0.44/1e6 + 1000 * 0.87/1e6
             = 0.001320 + 0.000870 = 0.002190
        """
        u = _usage(prompt=3000, cached=0, completion=1000)
        cost = compute_call_cost(u, VALIDATOR)
        assert cost == approx(0.002190, rel=1e-6)

    def test_validator_mixed(self) -> None:
        """Validator: 4000 prompt (2000 cached) + 1500 completion.
        uncached = 2000
        cost = 2000 * 0.44/1e6 + 2000 * 0.44/1e6 + 1500 * 0.87/1e6
             = 0.000880 + 0.000880 + 0.001305 = 0.003065
        """
        u = _usage(prompt=4000, cached=2000, completion=1500)
        cost = compute_call_cost(u, VALIDATOR)
        assert cost == approx(0.003065, rel=1e-6)

    # ── Edge cases ──

    def test_zero_tokens(self) -> None:
        """Zero tokens = zero cost."""
        u = _usage(prompt=0, cached=0, completion=0)
        cost = compute_call_cost(u, BRAIN)
        assert cost == 0.0

    def test_more_cached_than_prompt(self) -> None:
        """If cached > prompt, uncapped is clamped to 0."""
        u = _usage(prompt=100, cached=200, completion=50)
        cost = compute_call_cost(u, BRAIN)
        # uncached = 0, cached = 200 * 0.30/1e6, completion = 50 * 14.00/1e6
        # = 0 + 0.000060 + 0.000700 = 0.000760
        assert cost == approx(0.000760, rel=1e-6)

    def test_compute_details(self) -> None:
        """compute_call_details returns the breakdown."""
        u = _usage(prompt=2000, cached=500, completion=800)
        d = compute_call_details(u, BRAIN)
        assert d["uncached_prompt_tokens"] == 1500
        assert d["prompt_cost"] == approx(0.004200, rel=1e-6)
        assert d["cached_cost"] == approx(0.000150, rel=1e-6)
        assert d["completion_cost"] == approx(0.011200, rel=1e-6)
        assert d["total_cost"] == approx(0.015550, rel=1e-6)


# ── Cost tracker ──────────────────────────────────────────────────────────


class TestTracker:
    @pytest.fixture
    def tracker(self, tmp_path: Path) -> CostTracker:
        # The shipped default, from a fixture path — never the developer's
        # own user config (TD-1408).  These tests pin the shipped prices, so
        # a hand-pinned preset must not be able to break them.
        config_path = tmp_path / "config.yaml"
        config_path.write_text(default_config_yaml())
        cfg = load_config(config_path)
        return CostTracker(cfg)

    def test_begin_turn_resets_turn_cost(self, tracker: CostTracker) -> None:
        tracker.begin_turn()
        cost = tracker.record("brain", _usage(prompt=1000, cached=0, completion=500), BRAIN)
        assert cost == approx(0.009800, rel=1e-6)
        assert tracker.turn_cost() == approx(0.009800, rel=1e-6)

        tracker.begin_turn()
        assert tracker.turn_cost() == 0.0

    def test_turn_cost(self, tracker: CostTracker) -> None:
        tracker.begin_turn()
        tracker.record("brain", _usage(prompt=1000, cached=0, completion=500), BRAIN)
        tracker.record("brain", _usage(prompt=500, cached=0, completion=200), BRAIN)
        # 0.009800 + 0.002800 + 0.000700 = 0.003500
        # Wait: 500 * 2.80/1e6 + 200 * 14.00/1e6 = 0.001400 + 0.002800 = 0.004200
        # Total = 0.009800 + 0.004200 = 0.014000
        assert tracker.turn_cost() == approx(0.014000, rel=1e-6)

    def test_session_cost(self, tracker: CostTracker) -> None:
        tracker.begin_turn()
        tracker.record("brain", _usage(prompt=1000, cached=0, completion=500), BRAIN)
        tracker.begin_turn()
        tracker.record("worker", _usage(prompt=5000, cached=0, completion=2000), WORKER)
        # 0.009800 + 0.000690 = 0.010490
        assert tracker.session_cost() == approx(0.010490, rel=1e-6)

    def test_session_tokens(self, tracker: CostTracker) -> None:
        tracker.begin_turn()
        tracker.record("brain", _usage(prompt=1000, cached=0, completion=500), BRAIN)
        tracker.begin_turn()
        tracker.record("worker", _usage(prompt=5000, cached=0, completion=2000), WORKER)
        assert tracker.session_tokens() == 8500

    def test_turn_tokens(self, tracker: CostTracker) -> None:
        tracker.begin_turn()
        tracker.record("brain", _usage(prompt=1000, cached=0, completion=500), BRAIN)
        assert tracker.turn_tokens() == 1500

    def test_day_cost(self, tracker: CostTracker) -> None:
        """Day cost should equal session cost for a single-day session."""
        tracker.begin_turn()
        tracker.record("brain", _usage(prompt=1000, cached=0, completion=500), BRAIN)
        # day_cost should be >= session_cost (same calls, same day)
        assert tracker.day_cost() >= tracker.session_cost()

    def test_emit_cost_update(self, tracker: CostTracker) -> None:
        """emit_cost_update returns a CostUpdate with the right fields."""
        tracker.begin_turn()
        tracker.record("brain", _usage(prompt=1000, cached=0, completion=500), BRAIN)
        update = tracker.emit_cost_update("test-session")
        assert update.session_id == "test-session"
        assert update.turn_cost == approx(0.009800, rel=1e-6)
        assert update.session_cost == approx(0.009800, rel=1e-6)
        assert update.total_cost >= 0.0

    def test_record_returns_call_cost(self, tracker: CostTracker) -> None:
        """record() returns the cost of the call."""
        cost = tracker.record("brain", _usage(prompt=1000, cached=0, completion=500), BRAIN)
        assert cost == approx(0.009800, rel=1e-6)

    def test_calls_list(self, tracker: CostTracker) -> None:
        tracker.begin_turn()
        tracker.record("brain", _usage(prompt=100, cached=0, completion=50), BRAIN)
        tracker.record("worker", _usage(prompt=200, cached=0, completion=100), WORKER)
        assert len(tracker.calls) == 2
        assert tracker.calls[0].tier == "brain"
        assert tracker.calls[1].tier == "worker"

    def test_auto_lookup_tier_config(self, tracker: CostTracker) -> None:
        """Calling record without a cfg looks up from the model config."""
        tracker.begin_turn()
        cost = tracker.record("brain", _usage(prompt=1000, cached=0, completion=500))
        # Same as BRAIN cost since the default config's brain is kimi-k3
        assert cost == approx(0.009800, rel=1e-6)

    def test_summary(self, tracker: CostTracker) -> None:
        tracker.begin_turn()
        tracker.record("brain", _usage(prompt=1000, cached=0, completion=500), BRAIN)
        s = tracker.summary()
        assert s["turn_cost"] == approx(0.009800, rel=1e-6)
        assert s["turn_tokens"] == 1500
        assert s["call_count"] == 1
        assert s["session_cost"] == approx(0.009800, rel=1e-6)


# ── CallRecord ────────────────────────────────────────────────────────────


class TestCallRecord:
    def test_call_record_creation(self) -> None:
        record = CallRecord(
            tier="brain",
            model="moonshotai/kimi-k3",
            prompt_tokens=1000,
            cached_prompt_tokens=200,
            completion_tokens=500,
            uncached_prompt_tokens=800,
            prompt_cost=0.002240,
            cached_cost=0.000060,
            completion_cost=0.007000,
            cost=0.009300,
        )
        assert record.prompt_tokens == 1000
        assert record.cached_prompt_tokens == 200
        assert record.cost == 0.009300
