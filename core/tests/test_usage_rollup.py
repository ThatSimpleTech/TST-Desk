"""Tests for the usage view's two-dimensional rollup (TD-1706).

AGENTS §7 requires cost math to be tested with known token counts and
expected dollar figures, so the money assertions here are hand-computed
constants, never "the query returned something". Prices are fixed in
``_TIERS`` below and every expected dollar figure in this file is derived
from them on paper:

    brain     input $3.00 / cache $0.30 / output $15.00 per 1M tokens
    worker    input $0.80 / cache $0.08 / output  $4.00 per 1M tokens
    validator input $0.25 / cache $0.03 / output  $1.25 per 1M tokens

The per-call costs are produced by the real ``compute_call_cost``, so a
change to the pricing formula fails here too rather than only in
``test_cost.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from tstd.audit import AuditStore
from tstd.audit_queries import UsageRow, usage_rollup
from tstd.config import TierConfig
from tstd.cost import compute_call_cost
from tstd.provider import Usage

# 2026-08-12 is a Wednesday; the week it belongs to opened Monday 2026-08-10.
WED_TS = datetime(2026, 8, 12, 9, 0, 0).timestamp()
THU_TS = datetime(2026, 8, 13, 9, 0, 0).timestamp()
# The Sunday that closes that same week — the boundary case the week
# bucket gets wrong if `weekday 0` is used without stepping back.
SUN_TS = datetime(2026, 8, 16, 23, 0, 0).timestamp()
# The Monday that opens the next one.
NEXT_MON_TS = datetime(2026, 8, 17, 9, 0, 0).timestamp()

_TIERS = {
    "brain": TierConfig(
        slug="test/brain",
        base_url="https://example.invalid/v1",
        input_price=3.00,
        output_price=15.00,
        cache_read_price=0.30,
        context_window=200_000,
        max_output_tokens=8192,
    ),
    "worker": TierConfig(
        slug="test/worker",
        base_url="https://example.invalid/v1",
        input_price=0.80,
        output_price=4.00,
        cache_read_price=0.08,
        context_window=200_000,
        max_output_tokens=8192,
    ),
    "validator": TierConfig(
        slug="test/validator",
        base_url="https://example.invalid/v1",
        input_price=0.25,
        output_price=1.25,
        cache_read_price=0.03,
        context_window=200_000,
        max_output_tokens=8192,
    ),
}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[AuditStore]:
    s = AuditStore(tmp_path / "audit.db")
    yield s
    s.close()


def spend(
    store: AuditStore,
    *,
    session_id: str = "s1",
    tier: str = "brain",
    prompt: int,
    cached: int,
    completion: int,
    ts: float,
    is_classifier: bool = False,
) -> float:
    """Record one priced model call; returns the dollars it cost."""
    cost = compute_call_cost(
        Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            cached_prompt_tokens=cached,
        ),
        _TIERS[tier],
    )
    store.append_model_call(
        session_id=session_id,
        turn_id=None,
        tier=tier,
        model=_TIERS[tier].slug or tier,
        prompt_tokens=prompt,
        cached_prompt_tokens=cached,
        completion_tokens=completion,
        cost=cost,
        is_classifier=is_classifier,
        ts=ts,
    )
    return cost


def row(rows: list[UsageRow], key: str, tier: str) -> UsageRow:
    match = [r for r in rows if r.key == key and r.tier == tier]
    assert len(match) == 1, f"expected exactly one {key}/{tier} row, got {len(match)}"
    return match[0]


# ── Dollar figures ─────────────────────────────────────────────────────


def test_one_call_costs_the_hand_computed_amount(store: AuditStore) -> None:
    """100k prompt of which 40k cached, 5k completion, on the brain tier.

    uncached 60,000 x $3.00/1M  = $0.18
    cached   40,000 x $0.30/1M  = $0.012
    output    5,000 x $15.00/1M = $0.075
                                 --------
                                  $0.267
    """
    store.append_session("s1", "/ws", WED_TS)
    charged = spend(store, prompt=100_000, cached=40_000, completion=5_000, ts=WED_TS)
    assert charged == pytest.approx(0.267)

    got = row(usage_rollup(store, "session"), "s1", "brain")
    assert got.cost == pytest.approx(0.267)
    assert got.prompt_tokens == 100_000
    assert got.cached_prompt_tokens == 40_000
    assert got.completion_tokens == 5_000
    # Cached reads are tokens the model processed; the meter counts them.
    assert got.tokens == 145_000


def test_tiers_are_broken_out_and_priced_apart(store: AuditStore) -> None:
    """The same token counts on three tiers must not produce one number.

    brain     10,000 in (none cached) x $3.00/1M = $0.03
              2,000 out x $15.00/1M              = $0.03   -> $0.06
    worker    10,000 in x $0.80/1M               = $0.008
              2,000 out x $4.00/1M               = $0.008  -> $0.016
    validator 10,000 in x $0.25/1M               = $0.0025
              2,000 out x $1.25/1M               = $0.0025 -> $0.005
    """
    store.append_session("s1", "/ws", WED_TS)
    for tier in ("brain", "worker", "validator"):
        spend(store, tier=tier, prompt=10_000, cached=0, completion=2_000, ts=WED_TS)

    rows = usage_rollup(store, "session")
    assert [r.tier for r in rows] == ["brain", "validator", "worker"]  # tier-sorted
    assert row(rows, "s1", "brain").cost == pytest.approx(0.06)
    assert row(rows, "s1", "worker").cost == pytest.approx(0.016)
    assert row(rows, "s1", "validator").cost == pytest.approx(0.005)
    # And the split adds back up to the session's total spend.
    assert sum(r.cost for r in rows) == pytest.approx(0.081)


def test_cached_reads_are_priced_at_the_cache_rate(store: AuditStore) -> None:
    """Two calls, same token totals, differing only in what was cached.

    cold: 50,000 uncached x $3.00/1M = $0.15
    warm: 45,000 cached   x $0.30/1M = $0.0135
          5,000 uncached  x $3.00/1M = $0.015   -> $0.0285

    The rollup must show $0.1785, not 2 x $0.15.
    """
    store.append_session("s1", "/ws", WED_TS)
    spend(store, prompt=50_000, cached=0, completion=0, ts=WED_TS)
    spend(store, prompt=50_000, cached=45_000, completion=0, ts=WED_TS)

    got = row(usage_rollup(store, "session"), "s1", "brain")
    assert got.cost == pytest.approx(0.1785)
    assert got.prompt_tokens == 100_000
    assert got.cached_prompt_tokens == 45_000


def test_classifier_spend_stays_on_its_own_column(store: AuditStore) -> None:
    """TD-703: classifier dollars never fold into main-loop cost.

    loop:       20,000 in x $0.80/1M = $0.016
    classifier:  1,000 in x $0.80/1M = $0.0008
    """
    store.append_session("s1", "/ws", WED_TS)
    spend(store, tier="worker", prompt=20_000, cached=0, completion=0, ts=WED_TS)
    spend(
        store,
        tier="worker",
        prompt=1_000,
        cached=0,
        completion=0,
        ts=WED_TS,
        is_classifier=True,
    )

    got = row(usage_rollup(store, "session"), "s1", "worker")
    assert got.cost == pytest.approx(0.016)
    assert got.classifier_cost == pytest.approx(0.0008)
    # Tokens count both — the classifier really did move them.
    assert got.prompt_tokens == 21_000


# ── Buckets ────────────────────────────────────────────────────────────


def test_day_bucket_separates_days(store: AuditStore) -> None:
    store.append_session("s1", "/ws", WED_TS)
    spend(store, prompt=10_000, cached=0, completion=0, ts=WED_TS)  # $0.03
    spend(store, prompt=20_000, cached=0, completion=0, ts=THU_TS)  # $0.06

    rows = usage_rollup(store, "day")
    assert [r.key for r in rows] == ["2026-08-13", "2026-08-12"]  # newest first
    assert row(rows, "2026-08-12", "brain").cost == pytest.approx(0.03)
    assert row(rows, "2026-08-13", "brain").cost == pytest.approx(0.06)


def test_week_bucket_keys_on_the_monday_that_opened_it(store: AuditStore) -> None:
    """Wednesday, Thursday, and the Sunday that closes the week are one
    bucket; the following Monday opens a new one."""
    store.append_session("s1", "/ws", WED_TS)
    spend(store, prompt=10_000, cached=0, completion=0, ts=WED_TS)  # $0.03
    spend(store, prompt=20_000, cached=0, completion=0, ts=THU_TS)  # $0.06
    spend(store, prompt=10_000, cached=0, completion=0, ts=SUN_TS)  # $0.03
    spend(store, prompt=30_000, cached=0, completion=0, ts=NEXT_MON_TS)  # $0.09

    rows = usage_rollup(store, "week")
    assert [r.key for r in rows] == ["2026-08-17", "2026-08-10"]
    assert row(rows, "2026-08-10", "brain").cost == pytest.approx(0.12)
    assert row(rows, "2026-08-17", "brain").cost == pytest.approx(0.09)


def test_session_bucket_separates_sessions(store: AuditStore) -> None:
    store.append_session("s1", "/ws", WED_TS)
    store.append_session("s2", "/ws", THU_TS)
    spend(store, session_id="s1", prompt=10_000, cached=0, completion=0, ts=WED_TS)
    spend(store, session_id="s2", prompt=20_000, cached=0, completion=0, ts=THU_TS)

    rows = usage_rollup(store, "session")
    # Most recently active session first — ids do not sort chronologically,
    # so the ordering has to come from the spend, not from the key.
    assert [r.key for r in rows] == ["s2", "s1"]
    assert row(rows, "s1", "brain").cost == pytest.approx(0.03)
    assert row(rows, "s2", "brain").cost == pytest.approx(0.06)


def test_every_bucket_totals_the_same_dollars(store: AuditStore) -> None:
    """Session, day, and week slice identical rows — the totals must agree."""
    store.append_session("s1", "/ws", WED_TS)
    store.append_session("s2", "/ws", THU_TS)
    spend(store, session_id="s1", prompt=10_000, cached=0, completion=0, ts=WED_TS)
    spend(store, session_id="s2", tier="worker", prompt=50_000, cached=0, completion=0, ts=THU_TS)
    spend(store, session_id="s2", prompt=10_000, cached=0, completion=0, ts=NEXT_MON_TS)

    # $0.03 + $0.04 + $0.03
    for bucket in ("session", "day", "week"):
        rows = usage_rollup(store, bucket)  # type: ignore[arg-type]
        assert sum(r.cost for r in rows) == pytest.approx(0.10), bucket
        assert sum(r.tokens for r in rows) == 70_000, bucket


def test_empty_store_rolls_up_to_nothing(store: AuditStore) -> None:
    store.append_session("s1", "/ws", WED_TS)
    assert usage_rollup(store, "session") == []
    assert usage_rollup(store, "day") == []
    assert usage_rollup(store, "week") == []


# ── Limit ──────────────────────────────────────────────────────────────


def test_limit_keeps_the_most_recent_buckets(store: AuditStore) -> None:
    store.append_session("s1", "/ws", WED_TS)
    for offset in range(5):
        spend(store, prompt=10_000, cached=0, completion=0, ts=WED_TS + offset * 86400)

    rows = usage_rollup(store, "day", limit=2)
    assert [r.key for r in rows] == ["2026-08-16", "2026-08-15"]


def test_limit_never_truncates_a_bucket_mid_tier(store: AuditStore) -> None:
    """A cap on rows rather than buckets would report a day as costing
    less than it did; this is the test that would catch that."""
    store.append_session("s1", "/ws", WED_TS)
    for tier in ("brain", "worker", "validator"):
        spend(store, tier=tier, prompt=10_000, cached=0, completion=2_000, ts=WED_TS)
    spend(store, prompt=10_000, cached=0, completion=2_000, ts=THU_TS)

    rows = usage_rollup(store, "day", limit=1)
    assert [r.key for r in rows] == ["2026-08-13"]

    rows = usage_rollup(store, "day", limit=2)
    # Wednesday keeps all three of its tiers, not whatever fit.
    wednesday = [r for r in rows if r.key == "2026-08-12"]
    assert sorted(r.tier for r in wednesday) == ["brain", "validator", "worker"]
    assert sum(r.cost for r in wednesday) == pytest.approx(0.081)
