"""Tests for the audit writer (TD-902).

Covers: full-loop recording of turns and model calls, tool-call/result
pairing, refusal mapping (Class C), non-blocking producer paths, loud
degradation on write failure, and flushed shutdown.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from tests.test_loop import make_config, mock_factory, wait_for_turn
from tstd.audit import AuditStore
from tstd.audit_writer import AuditWriter
from tstd.cost import CallRecord
from tstd.loop import agent_loop
from tstd.mock import DEFAULT_CACHED_TOKENS, MockProvider, Script
from tstd.protocol import DecisionLogged, Error, TurnComplete
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner


@pytest.fixture
def store(tmp_path: Path) -> Iterator[AuditStore]:
    s = AuditStore(tmp_path / "audit.db")
    yield s
    s.close()


def make_record(tier: str = "brain", cost: float = 0.00166) -> CallRecord:
    return CallRecord(
        tier=tier,
        model="test-brain",
        prompt_tokens=1200,
        cached_prompt_tokens=1000,
        completion_tokens=480,
        uncached_prompt_tokens=200,
        prompt_cost=0.0002,
        cached_cost=0.0005,
        completion_cost=0.00096,
        cost=cost,
        timestamp=datetime.now(),
    )


# ── Full-loop recording ────────────────────────────────────────────────


async def test_loop_turn_and_model_call_are_recorded(store: AuditStore) -> None:
    """A real loop turn lands in turns + model_calls, linked by turn_id."""
    session = Session("/tmp/ws")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(session)

    mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="Hi")})
    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s, TierRouter(), mock_factory(mock), make_config(), audit_sink=writer
        ),
    )
    await runner.start()
    await session.add_user_message("hello")
    await wait_for_turn(session, 1)
    await writer._queue.join()  # drain without closing — store stays readable
    await runner.cancel()

    sessions = store._conn.execute("SELECT workspace_path FROM sessions").fetchall()
    assert sessions == [("/tmp/ws",)]

    turns = store._conn.execute(
        "SELECT id, turn_index, tier, tokens, cost, duration FROM turns"
    ).fetchall()
    assert len(turns) == 1
    turn_id, index, tier, tokens, cost, duration = turns[0]
    assert (index, tier) == (1, "brain")
    assert tokens > 0 and cost > 0 and duration >= 0

    calls = store._conn.execute(
        "SELECT turn_id, tier, model, prompt_tokens, cached_prompt_tokens,"
        " completion_tokens, cost, is_classifier FROM model_calls"
    ).fetchall()
    assert len(calls) == 1
    assert calls[0][0] == turn_id  # linked
    assert calls[0][1:6] == ("brain", "test-brain", 1200, DEFAULT_CACHED_TOKENS, 480)
    # brain prices in make_config: 200*1.0 + 1000*0.5 + 480*2.0, per million
    assert calls[0][6] == pytest.approx(0.00166)
    assert calls[0][7] == 0
    await writer.close()


async def test_multi_turn_indices_and_linkage(store: AuditStore) -> None:
    session = Session("/tmp/ws")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(session)
    mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="ok")})
    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s, TierRouter(), mock_factory(mock), make_config(), audit_sink=writer
        ),
    )
    await runner.start()
    await session.add_user_message("one")
    await wait_for_turn(session, 1)
    await session.add_user_message("two")
    await wait_for_turn(session, 2)
    await writer._queue.join()
    await runner.cancel()

    rows = store._conn.execute(
        "SELECT t.turn_index, COUNT(m.id) FROM turns t"
        " LEFT JOIN model_calls m ON m.turn_id = t.id GROUP BY t.id ORDER BY t.turn_index"
    ).fetchall()
    assert rows == [(1, 1), (2, 1)]
    await writer.close()


# ── Tool calls, refusals, decisions ────────────────────────────────────


async def test_tool_call_pairing_and_result_hash(store: AuditStore) -> None:
    session = Session("/tmp/ws")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(session)

    await session.event_log.add(
        ToolCallEvent(
            session_id=session.id,
            tool_call_id="tc1",
            name="fs_read",
            arguments={"path": "a.py"},
            decision_class="A",
            seq=1,
        )
    )
    await session.event_log.add(
        ToolResultEvent(
            session_id=session.id,
            tool_call_id="tc1",
            status="success",
            output="file body",
            seq=1,
        )
    )
    await writer._queue.join()

    row = store._conn.execute(
        "SELECT name, arguments, decision_class, status, result_hash FROM tool_calls"
    ).fetchone()
    assert row[0] == "fs_read"
    assert json.loads(row[1]) == {"path": "a.py"}
    assert row[2:5] == ("A", "success", hashlib.sha256(b"file body").hexdigest())
    await writer.close()


async def test_refused_boundary_violation_recorded_as_class_c(store: AuditStore) -> None:
    """TD-602: a guard refusal arrives as decision_class C + error result."""
    session = Session("/tmp/ws")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(session)

    await session.event_log.add(
        ToolCallEvent(
            session_id=session.id,
            tool_call_id="tc9",
            name="fs_write",
            arguments={"path": "../escape.py"},
            decision_class="C",
            seq=1,
        )
    )
    await session.event_log.add(
        ToolResultEvent(
            session_id=session.id,
            tool_call_id="tc9",
            status="error",
            output="refused: path outside workspace",
            seq=1,
        )
    )
    await writer._queue.join()

    row = store._conn.execute(
        "SELECT decision_class, status FROM tool_calls WHERE tool_call_id='tc9'"
    ).fetchone()
    assert row == ("C", "refused")
    await writer.close()


async def test_decision_logged_is_recorded(store: AuditStore) -> None:
    session = Session("/tmp/ws")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(session)
    await session.event_log.add(
        DecisionLogged(
            session_id=session.id,
            decision_class="B",
            what="ran pytest",
            why="user-approved test run",
            commit="deadbeef",
            seq=1,
        )
    )
    await writer._queue.join()
    row = store._conn.execute(
        "SELECT decision_class, what, why, commit_sha FROM decisions"
    ).fetchone()
    assert row == ("B", "ran pytest", "user-approved test run", "deadbeef")
    await writer.close()


async def test_orphan_result_is_recorded_not_dropped(store: AuditStore) -> None:
    session = Session("/tmp/ws")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(session)
    await session.event_log.add(
        ToolResultEvent(
            session_id=session.id,
            tool_call_id="ghost",
            status="error",
            output="no matching call",
            seq=1,
        )
    )
    await writer._queue.join()
    row = store._conn.execute(
        "SELECT name, status FROM tool_calls WHERE tool_call_id='ghost'"
    ).fetchone()
    assert row == ("<unknown>", "error")
    await writer.close()


# ── Classifier calls ───────────────────────────────────────────────────


async def test_classifier_call_recorded_immediately_without_turn(store: AuditStore) -> None:
    session = Session("/tmp/ws")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(session)
    writer.record_model_call(session.id, make_record(tier="worker"), is_classifier=True)
    await writer._queue.join()
    row = store._conn.execute("SELECT turn_id, tier, is_classifier FROM model_calls").fetchone()
    assert row == (None, "worker", 1)
    await writer.close()


async def test_cancelled_turn_still_records_spent_model_calls(store: AuditStore) -> None:
    """Cancel mid-turn emits no turn_complete — the calls are flushed
    unlinked on the terminal session_state instead of being lost."""
    from tstd.protocol import SessionState

    session = Session("/tmp/ws")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(session)
    writer.record_model_call(session.id, make_record(), is_classifier=False)
    await session.event_log.add(
        SessionState(session_id=session.id, state="cancelled", reason="test", seq=1)
    )
    await writer._queue.join()
    rows = store._conn.execute("SELECT turn_id, tier, cost FROM model_calls").fetchall()
    assert rows == [(None, "brain", 0.00166)]
    # No phantom turn row: the turn never completed.
    assert store._conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
    await writer.close()


# ── Non-blocking producers ─────────────────────────────────────────────


async def test_producers_never_block_on_store(tmp_path: Path) -> None:
    """Writes queue up while the drain is not running; producers still return
    immediately. The test completing without a running drain IS the non-
    blocking proof — any store I/O on the producer path would hang here."""
    store = AuditStore(tmp_path / "audit.db")
    session = Session("/tmp/ws")
    writer = AuditWriter(store)  # note: never start()ed
    writer.attach_session(session)
    for i in range(50):
        await session.event_log.add(
            ToolCallEvent(
                session_id=session.id,
                tool_call_id=f"tc{i}",
                name="fs_read",
                arguments={"path": f"{i}.py"},
                decision_class=None,
                seq=1,
            )
        )
        await session.event_log.add(
            ToolResultEvent(
                session_id=session.id,
                tool_call_id=f"tc{i}",
                status="success",
                output="x",
                seq=1,
            )
        )
        writer.record_model_call(session.id, make_record(), is_classifier=False)
    assert store._conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
    assert writer.backlog > 0

    writer.start()
    await writer._queue.join()
    assert store._conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 50
    # Buffered model calls are not flushed yet — no turn completed.
    assert store._conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
    # close() flushes them (turn_id unknown) before closing the store.
    await writer.close()
    verifier = AuditStore(tmp_path / "audit.db")
    try:
        rows = verifier._conn.execute("SELECT turn_id FROM model_calls").fetchall()
        assert rows == [(None,)] * 50
    finally:
        verifier.close()


# ── Loud degradation ───────────────────────────────────────────────────


class _FlakyStore(AuditStore):
    """Store whose turn writes fail while ``fail_writes`` is set."""

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.fail_writes = True

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
        if self.fail_writes:
            raise RuntimeError("disk full")
        return super().append_turn(session_id, turn_index, tier, tokens, cost, duration, ts)


async def test_write_failure_tells_the_user_once_per_burst(tmp_path: Path) -> None:
    store = _FlakyStore(tmp_path / "audit.db")
    session = Session("/tmp/ws")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(session)

    async def fire_turn() -> None:
        await session.event_log.add(
            TurnComplete(
                session_id=session.id,
                tokens=1,
                cost=0.001,
                tier="brain",
                duration=0.1,
                seq=1,
            )
        )

    await fire_turn()
    await fire_turn()
    await writer._queue.join()
    errors = [e for e in session.event_log.all_events if isinstance(e, Error)]
    assert len(errors) == 1  # edge-triggered: the burst reports once
    assert errors[0].code == "audit_write_failed"
    assert "Audit trail incomplete" in errors[0].message
    assert writer.degraded

    # Recovery: a successful write clears the edge...
    store.fail_writes = False
    await fire_turn()
    await writer._queue.join()
    assert not writer.degraded
    assert store._conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1

    # ...so the next failure burst reports again.
    store.fail_writes = True
    await fire_turn()
    await writer._queue.join()
    errors = [e for e in session.event_log.all_events if isinstance(e, Error)]
    assert len(errors) == 2
    await writer.close()  # writer owns the store's lifecycle
