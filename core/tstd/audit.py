"""Append-only audit store (TD-901).

Every turn, tool call, decision, and model call is recorded in SQLite at
``<user data dir>/audit.db``. The store is INSERT-only: no UPDATE or
DELETE statements exist anywhere in the codebase, asserted by a
source-level test in ``tests/test_audit.py``.

Secrets are scrubbed with the same redaction patterns as the logs
(:func:`tstd.logging.redact_secrets`) before anything is written —
prime directive §2.2.

The store is synchronous (sqlite3 is blocking I/O). Callers on the event
loop must wrap calls in ``asyncio.to_thread``; the TD-902 writer does.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from .logging import redact_secrets, redact_structure, user_data_dir

DecisionClass = Literal["A", "B", "C"]
ToolCallStatus = Literal["success", "error", "refused"]

# ── Schema ─────────────────────────────────────────────────────────────
#
# Six schema objects per TD-901: sessions, turns, tool_calls, decisions,
# model_calls — and `costs`, which is a VIEW over model_calls, not a
# table. Rollup rows would duplicate truth in a store that can never
# correct them; a view aggregates on read instead (TD-903 queries it).
#
# Timestamps are REAL seconds since the Unix epoch (UTC) — sortable, and
# groupable by local day via datetime(ts, 'unixepoch', 'localtime').

_SCHEMA_V1 = """
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    workspace_path TEXT NOT NULL,
    started_at REAL NOT NULL
);

CREATE TABLE turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    turn_index INTEGER NOT NULL,
    tier TEXT NOT NULL,
    tokens INTEGER NOT NULL,
    cost REAL NOT NULL,
    duration REAL NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX idx_turns_session ON turns(session_id, turn_index);

CREATE TABLE tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    tool_call_id TEXT NOT NULL,
    name TEXT NOT NULL,
    -- Arguments as JSON, scrubbed through redact_secrets before insert.
    arguments TEXT NOT NULL,
    -- NULL means the static rule table found the call ambiguous (TD-701).
    decision_class TEXT CHECK(decision_class IN ('A', 'B', 'C')),
    -- 'refused' is a boundary refusal — a Class C event the model never ran.
    status TEXT NOT NULL CHECK(status IN ('success', 'error', 'refused')),
    -- sha256 hex of the result output; NULL when there is no result.
    result_hash TEXT,
    ts REAL NOT NULL
);
CREATE INDEX idx_tool_calls_session ON tool_calls(session_id, ts);

CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    decision_class TEXT NOT NULL CHECK(decision_class IN ('A', 'B', 'C')),
    what TEXT NOT NULL,
    why TEXT NOT NULL,
    commit_sha TEXT NULL,
    ts REAL NOT NULL
);
CREATE INDEX idx_decisions_session ON decisions(session_id, ts);

CREATE TABLE model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    -- NULL for decision-classifier calls: they are not part of any turn.
    turn_id INTEGER REFERENCES turns(id),
    tier TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    cached_prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost REAL NOT NULL,
    is_classifier INTEGER NOT NULL DEFAULT 0 CHECK(is_classifier IN (0, 1)),
    ts REAL NOT NULL
);
CREATE INDEX idx_model_calls_session ON model_calls(session_id);
-- Day rollups group on ts; this index keeps them off a full table scan.
CREATE INDEX idx_model_calls_ts ON model_calls(ts);

CREATE VIEW costs AS
SELECT
    session_id,
    turn_id,
    tier,
    date(datetime(ts, 'unixepoch', 'localtime')) AS day,
    SUM(prompt_tokens) AS prompt_tokens,
    SUM(cached_prompt_tokens) AS cached_prompt_tokens,
    SUM(completion_tokens) AS completion_tokens,
    SUM(CASE WHEN is_classifier = 0 THEN cost ELSE 0 END) AS cost,
    SUM(CASE WHEN is_classifier = 1 THEN cost ELSE 0 END) AS classifier_cost
FROM model_calls
GROUP BY session_id, turn_id, tier, day;
"""

# v2 rebuilds `decisions` so commit_sha is nullable. The first machines
# that ran TD-901 got TEXT NOT NULL; later the source of _SCHEMA_V1 was
# edited in place to TEXT NULL without a new step, so those databases
# stayed NOT NULL and every Class B insert (commit is None) toasted
# audit_write_failed. Editing v1 does not migrate a database already at
# version 1 — this step does. (TD-1412)
_SCHEMA_V2 = """
CREATE TABLE decisions_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    decision_class TEXT NOT NULL CHECK(decision_class IN ('A', 'B', 'C')),
    what TEXT NOT NULL,
    why TEXT NOT NULL,
    commit_sha TEXT NULL,
    ts REAL NOT NULL
);
INSERT INTO decisions_v2 (id, session_id, decision_class, what, why, commit_sha, ts)
    SELECT id, session_id, decision_class, what, why, commit_sha, ts FROM decisions;
DROP TABLE decisions;
ALTER TABLE decisions_v2 RENAME TO decisions;
CREATE INDEX idx_decisions_session ON decisions(session_id, ts);
"""

# Ordered migration steps; index + 1 is the schema version that step
# produces. Every release that changes the schema appends one entry.
MIGRATIONS: tuple[str, ...] = (_SCHEMA_V1, _SCHEMA_V2)


# ── Scrubbing ──────────────────────────────────────────────────────────
#
# The recursive scrub lives in logging.redact_structure (TD-1405) so the
# audit store and the event pipeline share one implementation.


def _result_hash(output: str | None) -> str | None:
    """SHA-256 hex of the result output, or None when the call produced none."""
    if output is None:
        return None
    return hashlib.sha256(output.encode("utf-8")).hexdigest()


# ── Store ──────────────────────────────────────────────────────────────


class AuditStore:
    """INSERT-only handle over the audit database.

    Not thread-safe by itself; the writer serializes access. All methods
    are synchronous — wrap in ``asyncio.to_thread`` from the event loop.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the TD-902 writer drives the store via
        # asyncio.to_thread, whose worker pool is not one fixed thread.
        # Safety comes from the writer's single drain task serializing
        # every call, not from sqlite's thread affinity.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate(self._conn, MIGRATIONS)

    @classmethod
    def open_default(cls) -> AuditStore:
        """Open the store at the user data directory location."""
        return cls(user_data_dir() / "audit.db")

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def schema_version(self) -> int:
        """The highest applied migration version."""
        row = self._conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0])

    @staticmethod
    def _migrate(conn: sqlite3.Connection, migrations: Sequence[str]) -> None:
        """Apply any unapplied migrations in order, each in one transaction.

        Idempotent: a database already at the current version gets no
        writes. A failure inside a step rolls that step back and raises —
        the database is never left between versions.
        """
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        current = row[0] or 0
        for version, sql in enumerate(migrations, start=1):
            if version <= current:
                continue
            # executescript autocommits per statement, so the BEGIN/COMMIT
            # must live inside the script itself for all-or-nothing steps.
            stamp = (
                "INSERT INTO schema_migrations (version, applied_at) "
                f"VALUES ({version}, {time.time()});"
            )
            script = f"BEGIN;\n{sql}\n{stamp}\nCOMMIT;"
            try:
                conn.executescript(script)
            except sqlite3.Error:
                conn.rollback()
                raise

    def close(self) -> None:
        self._conn.close()

    # ── Appends ────────────────────────────────────────────────────────
    # One row per completed fact. Nothing is ever updated: a tool call is
    # recorded after it resolves, paired with its result, not patched.

    def append_session(self, session_id: str, workspace_path: str, started_at: float) -> None:
        """Record a session's start. Called once per session."""
        self._conn.execute(
            "INSERT INTO sessions (session_id, workspace_path, started_at) VALUES (?, ?, ?)",
            (session_id, workspace_path, started_at),
        )
        self._conn.commit()

    def append_turn(
        self,
        session_id: str,
        turn_index: int,
        tier: str,
        tokens: int,
        cost: float,
        duration: float,
        ts: float | None = None,
    ) -> int:
        """Record a completed turn; returns the row id for model_calls."""
        cur = self._conn.execute(
            "INSERT INTO turns (session_id, turn_index, tier, tokens, cost, duration, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, turn_index, tier, tokens, cost, duration, ts or time.time()),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def append_tool_call(
        self,
        session_id: str,
        tool_call_id: str,
        name: str,
        arguments: Mapping[str, Any],
        decision_class: DecisionClass | None,
        status: ToolCallStatus,
        result_output: str | None = None,
        ts: float | None = None,
    ) -> int:
        """Record a tool call and its outcome. Arguments are scrubbed first."""
        scrubbed = redact_structure(dict(arguments))
        cur = self._conn.execute(
            "INSERT INTO tool_calls (session_id, tool_call_id, name, arguments,"
            " decision_class, status, result_hash, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                tool_call_id,
                name,
                json.dumps(scrubbed, ensure_ascii=False, default=str),
                decision_class,
                status,
                _result_hash(result_output),
                ts or time.time(),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def append_decision(
        self,
        session_id: str,
        decision_class: DecisionClass,
        what: str,
        why: str,
        commit_sha: str | None,
        ts: float | None = None,
    ) -> int:
        """Record a classified decision (mirrors the TD-704 ledger entry).

        ``commit_sha`` is NULL for Class B decisions: the TD-704 rule
        binds Class A to a commit but Class B stands on judgment alone.

        ``what``/``why`` are free text and can quote tool output, so they
        are scrubbed like every other stored string (TD-4802) — this
        insert bypassed the redactor while ``append_tool_call`` scrubbed.
        """
        cur = self._conn.execute(
            "INSERT INTO decisions (session_id, decision_class, what, why, commit_sha, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                decision_class,
                redact_secrets(what),
                redact_secrets(why),
                commit_sha,
                ts or time.time(),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def append_model_call(
        self,
        session_id: str,
        turn_id: int | None,
        tier: str,
        model: str,
        prompt_tokens: int,
        cached_prompt_tokens: int,
        completion_tokens: int,
        cost: float,
        is_classifier: bool = False,
        ts: float | None = None,
    ) -> int:
        """Record one provider call with usage and computed cost."""
        cur = self._conn.execute(
            "INSERT INTO model_calls (session_id, turn_id, tier, model, prompt_tokens,"
            " cached_prompt_tokens, completion_tokens, cost, is_classifier, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                turn_id,
                tier,
                model,
                prompt_tokens,
                cached_prompt_tokens,
                completion_tokens,
                cost,
                1 if is_classifier else 0,
                ts or time.time(),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)
