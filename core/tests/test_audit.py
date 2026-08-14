"""Tests for the append-only audit store (TD-901)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from tstd.audit import MIGRATIONS, AuditStore

TSTD_ROOT = Path(__file__).resolve().parent.parent / "tstd"

# SQL mutation statements. Matches `DELETE FROM x` and `UPDATE x SET ...`
# while ignoring prose like "we never update rows".
_MUTATION_SQL = re.compile(r"\bdelete\s+from\s+\w|\bupdate\s+\w+\s+set\b", re.IGNORECASE)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[AuditStore]:
    s = AuditStore(tmp_path / "audit.db")
    s.append_session("s1", "/tmp/workspace", started_at=1000.0)
    yield s
    s.close()


# ── Schema ─────────────────────────────────────────────────────────────


def test_schema_has_all_six_objects(store: AuditStore) -> None:
    rows = store._conn.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        " AND name != 'schema_migrations'"
    ).fetchall()
    tables = {name for typ, name in rows if typ == "table"}
    views = {name for typ, name in rows if typ == "view"}
    assert tables == {"sessions", "turns", "tool_calls", "decisions", "model_calls"}
    assert views == {"costs"}


def test_indexes_exist_for_ui_queries(store: AuditStore) -> None:
    rows = store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    names = {r[0] for r in rows}
    assert {
        "idx_turns_session",
        "idx_tool_calls_session",
        "idx_decisions_session",
        "idx_model_calls_session",
        "idx_model_calls_ts",
    } <= names


def test_wal_mode_enabled(store: AuditStore) -> None:
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# ── Append-only ────────────────────────────────────────────────────────


def test_no_update_or_delete_statements_anywhere_in_tstd() -> None:
    """Source-level assertion: the codebase contains no mutation SQL.

    This is what makes the audit trail append-only in fact rather than
    by convention — a `DELETE FROM` or `UPDATE ... SET` in any tstd
    module fails this test.
    """
    offenders: list[str] = []
    for path in sorted(TSTD_ROOT.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _MUTATION_SQL.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == []


def test_store_exposes_no_update_methods(store: AuditStore) -> None:
    public = [m for m in dir(store) if not m.startswith("_")]
    assert public == [
        "append_decision",
        "append_model_call",
        "append_session",
        "append_tool_call",
        "append_turn",
        "close",
        "db_path",
        "open_default",
        "schema_version",
    ]


# ── Migrations ─────────────────────────────────────────────────────────


def test_fresh_database_migrates_to_current_version(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "audit.db")
    try:
        assert store.schema_version == len(MIGRATIONS)
        rows = store._conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [r[0] for r in rows] == list(range(1, len(MIGRATIONS) + 1))
    finally:
        store.close()


def test_reopen_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    first = AuditStore(path)
    first.append_session("s1", "/tmp/ws", started_at=1.0)
    first.close()
    second = AuditStore(path)
    try:
        assert second.schema_version == len(MIGRATIONS)
        count = second._conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert count == len(MIGRATIONS)  # no rows re-inserted
        workspaces = second._conn.execute("SELECT workspace_path FROM sessions").fetchall()
        assert workspaces == [("/tmp/ws",)]  # data survived
    finally:
        second.close()


def test_migration_applies_only_missing_steps() -> None:
    """A database at version N gets exactly steps N+1.. on migrate."""
    step1 = "CREATE TABLE t1 (id INTEGER PRIMARY KEY);"
    step2 = "CREATE TABLE t2 (id INTEGER PRIMARY KEY);"
    conn = sqlite3.connect(":memory:")
    AuditStore._migrate(conn, [step1])
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='t2'").fetchone() is None
    AuditStore._migrate(conn, [step1, step2])
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='t2'").fetchone() is not None


def test_failed_migration_rolls_back_atomically() -> None:
    good = "CREATE TABLE t1 (id INTEGER PRIMARY KEY);"
    bad = "INSERT INTO table_that_does_not_exist VALUES (1);"
    conn = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.Error):
        AuditStore._migrate(conn, [f"{good}\n{bad}"])
    # Neither the table nor the version stamp survived.
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='t1'").fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0


# ── Secret scrubbing (prime directive §2.2) ────────────────────────────


def test_tool_arguments_are_secret_scrubbed(store: AuditStore) -> None:
    # Secrets built dynamically — a key-shaped literal in this file would trip
    # the pre-commit secret scan, which is the control working as intended.
    secret_key = "sk-" + "p" * 24
    github_pat = "github_pat_" + "g" * 40
    aws_key = "AKIA" + "0" * 16
    args = {
        "command": f"curl -H 'Authorization: Bearer {secret_key}' https://api.example.com",
        "nested": {"github": github_pat},
        "list": [aws_key, "plain value"],
    }
    store.append_tool_call(
        session_id="s1",
        tool_call_id="tc1",
        name="shell",
        arguments=args,
        decision_class="C",
        status="refused",
        result_output=None,
    )
    raw = store._conn.execute("SELECT arguments FROM tool_calls").fetchone()[0]
    assert secret_key not in raw
    assert github_pat not in raw
    assert aws_key not in raw
    parsed = json.loads(raw)  # still valid JSON after scrubbing
    assert parsed["list"][0] == "[REDACTED]"
    assert parsed["nested"]["github"] == "[REDACTED]"
    assert parsed["list"][1] == "plain value"
    assert raw.count("[REDACTED]") == 3


def test_arguments_can_never_store_a_known_secret_pattern(store: AuditStore) -> None:
    from tstd.logging import SECRET_PATTERNS

    secrets = [
        ("sk-" + "a" * 24, "with a key-shaped prefix"),
        ("ghp_" + "b" * 38, "prefix"),
        ("-----BEGIN PRIVATE KEY-----", "exact"),
    ]
    for i, (secret, label) in enumerate(secrets):
        store.append_tool_call(
            session_id="s1",
            tool_call_id=f"tc{i}",
            name="fs_write",
            arguments={"note": label, "payload": secret},
            decision_class="A",
            status="success",
        )
    blob = "\n".join(
        r[0] for r in store._conn.execute("SELECT arguments FROM tool_calls").fetchall()
    )
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(blob), f"pattern {pattern.pattern!r} survived in audit rows"


def test_audit_store_matches_log_redaction(store: AuditStore) -> None:
    """The audit path scrubs with the same redactor as the log path."""
    from tstd.logging import redact_secrets

    sample = f"token=sk-{'x' * 30} and key {'AKIA' + '0' * 16}"
    store.append_tool_call(
        session_id="s1",
        tool_call_id="tc-log",
        name="shell",
        arguments={"cmd": sample},
        decision_class=None,
        status="success",
    )
    stored = json.loads(
        store._conn.execute(
            "SELECT arguments FROM tool_calls WHERE tool_call_id='tc-log'"
        ).fetchone()[0]
    )
    assert stored["cmd"] == redact_secrets(sample)


# ── Records ────────────────────────────────────────────────────────────


def test_turn_round_trip(store: AuditStore) -> None:
    row_id = store.append_turn(
        session_id="s1",
        turn_index=0,
        tier="brain",
        tokens=1500,
        cost=0.0042,
        duration=2.5,
        ts=2000.0,
    )
    row = store._conn.execute(
        "SELECT id, session_id, turn_index, tier, tokens, cost, duration, ts FROM turns"
    ).fetchone()
    assert row == (row_id, "s1", 0, "brain", 1500, 0.0042, 2.5, 2000.0)


def test_tool_call_result_hash(store: AuditStore) -> None:
    output = "file contents here"
    store.append_tool_call(
        session_id="s1",
        tool_call_id="tc1",
        name="fs_read",
        arguments={"path": "a.py"},
        decision_class="A",
        status="success",
        result_output=output,
    )
    row = store._conn.execute(
        "SELECT result_hash, status FROM tool_calls WHERE tool_call_id='tc1'"
    ).fetchone()
    assert row[0] == hashlib.sha256(output.encode("utf-8")).hexdigest()
    assert row[1] == "success"


def test_tool_call_without_result_has_null_hash(store: AuditStore) -> None:
    store.append_tool_call(
        session_id="s1",
        tool_call_id="tc2",
        name="fs_write",
        arguments={"path": "AGENTS.md"},
        decision_class="C",
        status="refused",
        result_output=None,
    )
    row = store._conn.execute(
        "SELECT result_hash, decision_class, status FROM tool_calls WHERE tool_call_id='tc2'"
    ).fetchone()
    assert row == (None, "C", "refused")


def test_decision_round_trip(store: AuditStore) -> None:
    store.append_decision(
        session_id="s1",
        decision_class="A",
        what="edited src/app.py",
        why="in-workspace edit within writable_paths",
        commit_sha="abc123",
        ts=3000.0,
    )
    row = store._conn.execute(
        "SELECT session_id, decision_class, what, why, commit_sha, ts FROM decisions"
    ).fetchone()
    assert row == (
        "s1",
        "A",
        "edited src/app.py",
        "in-workspace edit within writable_paths",
        "abc123",
        3000.0,
    )


def test_model_call_round_trip_with_classifier_flag(store: AuditStore) -> None:
    turn_id = store.append_turn(
        session_id="s1",
        turn_index=0,
        tier="brain",
        tokens=10,
        cost=0.1,
        duration=1.0,
    )
    store.append_model_call(
        session_id="s1",
        turn_id=turn_id,
        tier="brain",
        model="test-model",
        prompt_tokens=900,
        cached_prompt_tokens=500,
        completion_tokens=100,
        cost=0.01,
        ts=4000.0,
    )
    store.append_model_call(
        session_id="s1",
        turn_id=None,
        tier="worker",
        model="test-model-small",
        prompt_tokens=200,
        cached_prompt_tokens=0,
        completion_tokens=5,
        cost=0.0001,
        is_classifier=True,
        ts=4001.0,
    )
    rows = store._conn.execute(
        "SELECT turn_id, tier, is_classifier FROM model_calls ORDER BY ts"
    ).fetchall()
    assert rows == [(turn_id, "brain", 0), (None, "worker", 1)]


def test_costs_view_aggregates_model_calls(store: AuditStore) -> None:
    """Pre-verifies the data source TD-903's aggregation queries read."""
    turn_id = store.append_turn(
        session_id="s1",
        turn_index=0,
        tier="brain",
        tokens=10,
        cost=0.1,
        duration=1.0,
    )
    store.append_model_call(
        session_id="s1",
        turn_id=turn_id,
        tier="brain",
        model="m",
        prompt_tokens=1000,
        cached_prompt_tokens=200,
        completion_tokens=100,
        cost=0.05,
    )
    store.append_model_call(
        session_id="s1",
        turn_id=turn_id,
        tier="brain",
        model="m",
        prompt_tokens=500,
        cached_prompt_tokens=500,
        completion_tokens=50,
        cost=0.02,
    )
    store.append_model_call(
        session_id="s1",
        turn_id=None,
        tier="worker",
        model="m-small",
        prompt_tokens=100,
        cached_prompt_tokens=0,
        completion_tokens=10,
        cost=0.001,
        is_classifier=True,
    )
    rows = store._conn.execute(
        "SELECT tier, prompt_tokens, cached_prompt_tokens, completion_tokens, cost,"
        " classifier_cost, day FROM costs ORDER BY tier"
    ).fetchall()
    assert len(rows) == 2
    brain, worker = rows
    assert brain[0] == "brain"
    assert (brain[1], brain[2], brain[3]) == (1500, 700, 150)
    assert brain[4] == pytest.approx(0.07)
    assert brain[5] == pytest.approx(0.0)
    assert brain[6] is not None  # day bucket computed
    assert worker[0] == "worker"
    assert worker[4] == pytest.approx(0.0)  # classifier cost stays separate
    assert worker[5] == pytest.approx(0.001)


def test_foreign_keys_enforced(store: AuditStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.append_turn(
            session_id="no-such-session",
            turn_index=0,
            tier="brain",
            tokens=1,
            cost=0.0,
            duration=0.1,
        )
