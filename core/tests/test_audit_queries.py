"""Tests for cost aggregation and export (TD-903).

The heart of the suite is a seeded-random property test: dozens of
model-call records with random tiers, sessions, timestamps, and costs
are inserted, and every aggregation query is checked against the
same sums computed in plain Python. Aggregates that drifted from the
individual records would fail it.
"""

from __future__ import annotations

import csv
import json
import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tstd.audit import AuditStore
from tstd.audit_queries import (
    CostAggregate,
    cost_by_day,
    cost_by_session,
    cost_by_tier,
    cost_by_turn,
    export_csv,
    export_jsonl,
)

TODAY_TS = datetime(2026, 8, 10, 12, 0, 0).timestamp()  # a Monday; spans ~5 days below


@pytest.fixture
def store(tmp_path: Path) -> Iterator[AuditStore]:
    s = AuditStore(tmp_path / "audit.db")
    yield s
    s.close()


def add_model_call(
    store: AuditStore,
    *,
    session_id: str = "s1",
    turn_id: int | None = None,
    tier: str = "brain",
    model: str = "test-model",
    prompt: int = 100,
    cached: int = 50,
    completion: int = 25,
    cost: float = 0.001,
    is_classifier: bool = False,
    ts: float = TODAY_TS,
) -> int:
    return store.append_model_call(
        session_id=session_id,
        turn_id=turn_id,
        tier=tier,
        model=model,
        prompt_tokens=prompt,
        cached_prompt_tokens=cached,
        completion_tokens=completion,
        cost=cost,
        is_classifier=is_classifier,
        ts=ts,
    )


# ── Unit behavior ──────────────────────────────────────────────────────


def test_empty_store_has_no_aggregates(store: AuditStore) -> None:
    store.append_session("s1", "/ws", TODAY_TS)
    assert cost_by_session(store) == []
    assert cost_by_turn(store, "s1") == []
    assert cost_by_day(store) == []
    assert cost_by_tier(store) == []


def test_aggregates_have_the_right_scope_and_key(store: AuditStore) -> None:
    store.append_session("s1", "/ws", TODAY_TS)
    turn_id = store.append_turn("s1", 1, "brain", 100, 0.001, 1.0, ts=TODAY_TS)
    add_model_call(store, turn_id=turn_id)

    by_turn = cost_by_turn(store, "s1")
    assert by_turn[0].scope == "turn"
    assert by_turn[0].key == str(turn_id)
    assert cost_by_session(store)[0].key == "s1"
    assert cost_by_day(store)[0].key == "2026-08-10"
    assert cost_by_tier(store)[0].key == "brain"


def test_classifier_spend_stays_separate(store: AuditStore) -> None:
    """TD-703: classifier cost must never fold into main-loop cost."""
    store.append_session("s1", "/ws", TODAY_TS)
    turn_id = store.append_turn("s1", 1, "brain", 100, 0.002, 1.0, ts=TODAY_TS)
    add_model_call(store, turn_id=turn_id, cost=0.002)
    add_model_call(store, tier="worker", cost=0.0004, is_classifier=True)

    by_session = cost_by_session(store)[0]
    assert by_session.cost == pytest.approx(0.002)
    assert by_session.classifier_cost == pytest.approx(0.0004)

    # Classifier calls have no turn — never under cost_by_turn.
    by_turn = cost_by_turn(store, "s1")
    assert len(by_turn) == 1
    assert by_turn[0].cost == pytest.approx(0.002)
    assert by_turn[0].classifier_cost == pytest.approx(0.0)


def test_session_filter_on_day_and_tier(store: AuditStore) -> None:
    store.append_session("s1", "/ws", TODAY_TS)
    store.append_session("s2", "/ws", TODAY_TS)
    add_model_call(store, session_id="s1", tier="brain", cost=0.001)
    add_model_call(store, session_id="s2", tier="worker", cost=0.002)

    by_day = cost_by_day(store, "s1")
    assert [a.key for a in by_day] == ["2026-08-10"]
    assert by_day[0].cost == pytest.approx(0.001)

    by_tier = cost_by_tier(store, "s1")
    assert [a.key for a in by_tier] == ["brain"]


def test_multi_session_rolls_up(store: AuditStore) -> None:
    store.append_session("s1", "/ws", TODAY_TS)
    store.append_session("s2", "/ws", TODAY_TS)
    add_model_call(store, session_id="s1", cost=0.001)
    add_model_call(store, session_id="s2", cost=0.002)

    by_session = cost_by_session(store)
    assert [a.key for a in by_session] == ["s1", "s2"]
    assert [a.cost for a in by_session] == [pytest.approx(0.001), pytest.approx(0.002)]


# ── Exports ────────────────────────────────────────────────────────────


def test_export_jsonl_round_trips(store: AuditStore, tmp_path: Path) -> None:
    store.append_session("s1", "/ws", TODAY_TS)
    turn_id = store.append_turn("s1", 1, "brain", 175, 0.00035, 1.0, ts=TODAY_TS)
    add_model_call(store, turn_id=turn_id, cost=0.00035, ts=TODAY_TS + 1)
    add_model_call(store, tier="worker", cost=0.0004, is_classifier=True, ts=TODAY_TS + 2)

    out = tmp_path / "exports" / "calls.jsonl"
    written = export_jsonl(store, out)
    assert written == 2

    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(lines) == 2
    # Ordered by ts: the turn-linked call first.
    assert lines[0]["turn_id"] == turn_id
    assert lines[0]["cost"] == pytest.approx(0.00035)
    assert lines[1]["is_classifier"] == 1
    assert lines[1]["turn_id"] is None
    # Timestamps are ISO-8601 UTC, not raw epoch.
    assert lines[0]["ts"].endswith("+00:00")
    assert datetime.fromisoformat(lines[0]["ts"]) == datetime.fromtimestamp(TODAY_TS + 1, tz=UTC)


def test_export_csv_has_header_and_rows(store: AuditStore, tmp_path: Path) -> None:
    store.append_session("s1", "/ws", TODAY_TS)
    add_model_call(store, cost=0.001)

    out = tmp_path / "calls.csv"
    written = export_csv(store, out)
    assert written == 1

    with out.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["tier"] == "brain"
    assert float(rows[0]["cost"]) == pytest.approx(0.001)
    assert rows[0]["session_id"] == "s1"


def test_export_empty_is_header_only(store: AuditStore, tmp_path: Path) -> None:
    store.append_session("s1", "/ws", TODAY_TS)
    csv_out = tmp_path / "empty.csv"
    jsonl_out = tmp_path / "empty.jsonl"
    assert export_csv(store, csv_out) == 0
    assert export_jsonl(store, jsonl_out) == 0
    assert csv_out.read_text().count("\n") == 1  # header only
    assert jsonl_out.read_text() == ""


# ── Property test ──────────────────────────────────────────────────────

_TIERS = ("brain", "worker", "validator")


def _local_day(ts: float) -> str:
    """The same day bucket the view computes — localtime, like SQLite."""
    return datetime.fromtimestamp(ts).date().isoformat()


@dataclass
class _Record:
    session_id: str
    turn_id: int | None
    tier: str
    prompt: int
    cached: int
    completion: int
    cost: float
    is_classifier: bool
    ts: float


def test_aggregates_equal_sum_of_records(store: AuditStore) -> None:
    """Seeded-random records: every aggregation scope must equal the
    Python-side sum of the individual records."""
    rng = random.Random(20260813)
    sessions = ("s1", "s2")
    for sid in sessions:
        store.append_session(sid, "/ws", TODAY_TS)
    turns = {
        sid: [
            store.append_turn(sid, i + 1, "brain", 100, 0.001, 1.0, ts=TODAY_TS) for i in range(3)
        ]
        for sid in sessions
    }

    # Insert 60 random records; keep the exact inserts for comparison.
    records: list[_Record] = []
    for _ in range(60):
        sid = rng.choice(sessions)
        is_classifier = rng.random() < 0.25
        rec = _Record(
            session_id=sid,
            turn_id=None if is_classifier else rng.choice(turns[sid]),
            tier=rng.choice(_TIERS),
            prompt=rng.randrange(0, 5000),
            cached=rng.randrange(0, 3000),
            completion=rng.randrange(0, 1000),
            cost=rng.randrange(0, 10000) / 10_000_000,
            is_classifier=is_classifier,
            ts=TODAY_TS + rng.random() * 5 * 86400,
        )
        store.append_model_call(
            session_id=rec.session_id,
            turn_id=rec.turn_id,
            tier=rec.tier,
            model="test-model",
            prompt_tokens=rec.prompt,
            cached_prompt_tokens=rec.cached,
            completion_tokens=rec.completion,
            cost=rec.cost,
            is_classifier=rec.is_classifier,
            ts=rec.ts,
        )
        records.append(rec)

    def expected(rows: list[_Record]) -> tuple[int, int, int, float, float]:
        return (
            sum(r.prompt for r in rows),
            sum(r.cached for r in rows),
            sum(r.completion for r in rows),
            sum(r.cost for r in rows if not r.is_classifier),
            sum(r.cost for r in rows if r.is_classifier),
        )

    def assert_matches(actual: CostAggregate, rows: list[_Record]) -> None:
        prompt, cached, completion, cost, classifier = expected(rows)
        assert actual.prompt_tokens == prompt
        assert actual.cached_prompt_tokens == cached
        assert actual.completion_tokens == completion
        assert actual.cost == pytest.approx(cost)
        assert actual.classifier_cost == pytest.approx(classifier)

    # by_session
    for aggregate in cost_by_session(store):
        assert_matches(aggregate, [r for r in records if r.session_id == aggregate.key])
    # by_turn (classifier rows excluded on both sides)
    for aggregate in cost_by_turn(store, "s1"):
        assert_matches(aggregate, [r for r in records if r.turn_id == int(aggregate.key)])
    # by_day — Python and SQLite must agree on local day bucketing
    for aggregate in cost_by_day(store):
        assert_matches(aggregate, [r for r in records if _local_day(r.ts) == aggregate.key])
    # by_tier
    for aggregate in cost_by_tier(store):
        assert_matches(aggregate, [r for r in records if r.tier == aggregate.key])
    # and a cross-scope invariant: token totals over all sessions
    # equal the day totals and the tier totals.
    total_prompt = sum(a.prompt_tokens for a in cost_by_session(store))
    assert sum(a.prompt_tokens for a in cost_by_day(store)) == total_prompt
    assert sum(a.prompt_tokens for a in cost_by_tier(store)) == total_prompt
