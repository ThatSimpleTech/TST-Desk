"""Cost aggregation and export over the audit store (TD-903).

All aggregation reads through the ``costs`` view (TD-901), so every
answer is computed from the same per-call rows the totals come from —
there is no rollup table to disagree with. Classifier spend (TD-703) is
kept on its own column, never folded into main-loop cost.

Exports carry the individual ``model_calls`` records — the source of
truth — in JSONL or CSV form for spreadsheets and billing audits.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .audit import AuditStore

Scope = Literal["turn", "session", "day", "tier"]
UsageBucket = Literal["session", "day", "week"]


@dataclass(frozen=True)
class CostAggregate:
    """Cost and token totals for one bucket of one scope."""

    scope: Scope
    key: str  # turn row id, session id, ISO day, or tier name
    prompt_tokens: int
    cached_prompt_tokens: int
    completion_tokens: int
    cost: float
    classifier_cost: float


def _aggregate(
    conn: sqlite3.Connection, scope: Scope, key_expr: str, where: str, params: tuple[str, ...]
) -> list[CostAggregate]:
    rows = conn.execute(
        f"SELECT {key_expr}, SUM(prompt_tokens), SUM(cached_prompt_tokens),"
        f" SUM(completion_tokens), SUM(cost), SUM(classifier_cost)"
        f" FROM costs {where} GROUP BY {key_expr} ORDER BY {key_expr}",
        params,
    ).fetchall()
    return [
        CostAggregate(
            scope=scope,
            key=str(key),
            prompt_tokens=prompt,
            cached_prompt_tokens=cached,
            completion_tokens=completion,
            cost=cost,
            classifier_cost=classifier,
        )
        for key, prompt, cached, completion, cost, classifier in rows
    ]


def cost_by_turn(store: AuditStore, session_id: str) -> list[CostAggregate]:
    """Cost per turn within a session (classifier calls have no turn — excluded)."""
    return _aggregate(
        store._conn,
        "turn",
        "turn_id",
        "WHERE session_id = ? AND turn_id IS NOT NULL",
        (session_id,),
    )


def cost_by_session(store: AuditStore) -> list[CostAggregate]:
    """Cost per session, classifier spend on its own column."""
    return _aggregate(store._conn, "session", "session_id", "", ())


def cost_by_day(store: AuditStore, session_id: str | None = None) -> list[CostAggregate]:
    """Cost per local day; the day boundary comes from the view's localtime bucket."""
    if session_id is None:
        return _aggregate(store._conn, "day", "day", "", ())
    return _aggregate(store._conn, "day", "day", "WHERE session_id = ?", (session_id,))


def cost_by_tier(store: AuditStore, session_id: str | None = None) -> list[CostAggregate]:
    """Cost per brain/worker/validator tier."""
    if session_id is None:
        return _aggregate(store._conn, "tier", "tier", "", ())
    return _aggregate(store._conn, "tier", "tier", "WHERE session_id = ?", (session_id,))


# ── Two-dimensional rollup (TD-1706) ───────────────────────────────────
#
# The four queries above answer one scope at a time, which is what the
# totals needed. The usage view needs a bucket *and* its tier split at
# once — "what did Tuesday cost, and how much of that was the brain" —
# so this groups on both. Same `costs` view, same classifier separation:
# it is another way to slice the identical rows, never a second source.


@dataclass(frozen=True)
class UsageRow:
    """One bucket's spend on one tier."""

    bucket: UsageBucket
    key: str  # session id, ISO day, or the ISO day the week opened on
    tier: str
    prompt_tokens: int
    cached_prompt_tokens: int
    completion_tokens: int
    cost: float
    classifier_cost: float

    @property
    def tokens(self) -> int:
        """Every token the bucket moved, cached reads included."""
        return self.prompt_tokens + self.cached_prompt_tokens + self.completion_tokens


# Weeks open on Monday: step forward to the coming Sunday, then back six
# days. SQLite's `weekday` modifier leaves a date that already is Sunday
# where it stands, so Sunday resolves to the Monday that opened its own
# week rather than the one opening the next.
_BUCKET_KEY: dict[UsageBucket, str] = {
    "session": "session_id",
    "day": "day",
    "week": "date(day, 'weekday 0', '-6 days')",
}


def usage_rollup(
    store: AuditStore, bucket: UsageBucket, limit: int | None = None
) -> list[UsageRow]:
    """Token and cost totals per ``bucket`` per tier, most recent bucket first.

    ``limit`` caps the number of *buckets* returned, never the number of
    rows — a cap applied to rows would truncate a bucket mid-tier and
    report a day as costing less than it did.
    """
    key_expr = _BUCKET_KEY[bucket]
    rows = store._conn.execute(
        f"SELECT {key_expr}, tier, SUM(prompt_tokens), SUM(cached_prompt_tokens),"
        " SUM(completion_tokens), SUM(cost), SUM(classifier_cost), MAX(day)"
        f" FROM costs GROUP BY {key_expr}, tier"
    ).fetchall()

    # Recency of a bucket is the latest day any of its tiers spent on.
    # Session ids do not sort chronologically, so the key alone cannot
    # order them; the day can, for all three bucket kinds.
    latest: dict[str, str] = {}
    for key, _tier, _p, _c, _o, _cost, _cls, day in rows:
        k = str(key)
        latest[k] = max(latest.get(k, ""), str(day))
    keys = sorted(latest, key=lambda k: (latest[k], k), reverse=True)
    if limit is not None:
        keys = keys[:limit]
    rank = {k: i for i, k in enumerate(keys)}

    out = [
        UsageRow(
            bucket=bucket,
            key=str(key),
            tier=str(tier),
            prompt_tokens=prompt,
            cached_prompt_tokens=cached,
            completion_tokens=completion,
            cost=cost,
            classifier_cost=classifier,
        )
        for key, tier, prompt, cached, completion, cost, classifier, _day in rows
        if str(key) in rank
    ]
    out.sort(key=lambda r: (rank[r.key], r.tier))
    return out


# ── Export ─────────────────────────────────────────────────────────────

_EXPORT_COLUMNS = (
    "session_id",
    "turn_id",
    "tier",
    "model",
    "prompt_tokens",
    "cached_prompt_tokens",
    "completion_tokens",
    "cost",
    "is_classifier",
    "ts",
)


def _export_rows(store: AuditStore) -> list[dict[str, object]]:
    """Individual model-call records with ISO-8601 UTC timestamps."""
    cols = ", ".join(_EXPORT_COLUMNS)
    rows = store._conn.execute(f"SELECT {cols} FROM model_calls ORDER BY ts, id").fetchall()
    out: list[dict[str, object]] = []
    for row in rows:
        record = dict(zip(_EXPORT_COLUMNS, row, strict=True))
        record["ts"] = datetime.fromtimestamp(float(record["ts"]), tz=UTC).isoformat()
        out.append(record)
    return out


def export_jsonl(store: AuditStore, path: Path) -> int:
    """Write one JSON object per model-call record. Returns rows written."""
    rows = _export_rows(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in rows:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(rows)


def export_csv(store: AuditStore, path: Path) -> int:
    """Write a header row plus one line per model-call record. Returns data rows."""
    rows = _export_rows(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(_EXPORT_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
