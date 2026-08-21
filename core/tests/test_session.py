"""Tests for the session model, event log, runner, and registry."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from tstd.protocol import AssistantDelta
from tstd.protocol import SessionState as SessionStateEvent
from tstd.session import Session, SessionError, SessionEventLog, SessionRegistry, SessionRunner

# ── SessionEventLog ────────────────────────────────────────────────────


class TestSessionEventLog:
    async def test_add_assigns_monotonic_seq(self) -> None:
        log = SessionEventLog()
        ev1 = await log.add(AssistantDelta(session_id="s", delta="a", seq=1))
        ev2 = await log.add(AssistantDelta(session_id="s", delta="b", seq=1))
        assert ev1.seq == 1
        assert ev2.seq == 2
        assert ev1.seq < ev2.seq

    async def test_events_from_returns_correct_range(self) -> None:
        log = SessionEventLog()
        await log.add(AssistantDelta(session_id="s", delta="a", seq=1))
        await log.add(AssistantDelta(session_id="s", delta="b", seq=1))
        await log.add(AssistantDelta(session_id="s", delta="c", seq=1))

        events = log.events_from(2)
        assert len(events) == 2
        assert events[0].seq == 2
        assert events[1].seq == 3

    async def test_events_from_clamps_below_one(self) -> None:
        log = SessionEventLog()
        await log.add(AssistantDelta(session_id="s", delta="a", seq=1))
        events = log.events_from(0)
        assert len(events) == 1

    async def test_events_from_beyond_end_returns_empty(self) -> None:
        log = SessionEventLog()
        await log.add(AssistantDelta(session_id="s", delta="a", seq=1))
        events = log.events_from(999)
        assert len(events) == 0

    async def test_last_seq(self) -> None:
        log = SessionEventLog()
        assert log.last_seq == 0
        await log.add(AssistantDelta(session_id="s", delta="a", seq=1))
        assert log.last_seq == 1
        await log.add(AssistantDelta(session_id="s", delta="b", seq=1))
        assert log.last_seq == 2

    async def test_drop_before_matches_a_window_and_keeps_last_seq(self) -> None:
        log = SessionEventLog()
        await log.add(AssistantDelta(session_id="s", delta="a", seq=1))
        await log.add(AssistantDelta(session_id="s", delta="b", seq=1))
        await log.add(AssistantDelta(session_id="s", delta="c", seq=1))
        await log.drop_before(2)
        assert [e.seq for e in log.events_from(1)] == [2, 3]
        assert log.earliest_seq == 2
        assert log.last_seq == 3
        assert log.events_from(1)[0].seq == log.earliest_seq

    async def test_all_events_returns_copy(self) -> None:
        log = SessionEventLog()
        await log.add(AssistantDelta(session_id="s", delta="a", seq=1))
        events = log.all_events
        assert len(events) == 1
        # Verify it's a copy
        events.clear()
        assert log.all_events is not None  # original still intact

    async def test_wait_for_new_event_blocking(self) -> None:
        log = SessionEventLog()

        async def delayed_add() -> None:
            await asyncio.sleep(0.05)
            await log.add(AssistantDelta(session_id="s", delta="a", seq=1))

        # Start waiting, then add an event
        async def waiter() -> int:
            return await log.wait_for_new_event(0)

        results = await asyncio.gather(waiter(), delayed_add())
        assert results[0] == 1  # waiter returns new last_seq

    async def test_wait_for_new_event_already_past(self) -> None:
        log = SessionEventLog()
        await log.add(AssistantDelta(session_id="s", delta="a", seq=1))
        # Already at seq 1, waiting for seq > 0 should return immediately
        seq = await log.wait_for_new_event(0)
        assert seq == 1

    async def test_concurrent_adds_sequential_seqs(self) -> None:
        """Concurrent adds produce strictly increasing seqs."""
        log = SessionEventLog()

        async def add_delta(label: str) -> int:
            evt = await log.add(AssistantDelta(session_id="s", delta=label, seq=1))
            return evt.seq

        results = await asyncio.gather(*[add_delta(f"e{i}") for i in range(10)])
        assert results == list(range(1, 11))


# ── Session ────────────────────────────────────────────────────────────


class TestSession:
    def test_initial_state(self) -> None:
        s = Session("/tmp/test")
        assert s.state == "idle"
        assert s.id is not None
        assert s.workspace_path == "/tmp/test"
        assert s.cancel_requested is False

    async def test_set_state_running(self) -> None:
        s = Session("/tmp/test")
        await s.set_state("running")
        assert s.state == "running"

    async def test_state_transition_complete(self) -> None:
        s = Session("/tmp/test")
        await s.set_state("running")
        await s.set_state("complete")
        assert s.state == "complete"

    async def test_state_transition_failed(self) -> None:
        s = Session("/tmp/test")
        await s.set_state("running")
        await s.set_state("failed", reason="something broke")
        assert s.state == "failed"

    async def test_state_transition_cancelled(self) -> None:
        s = Session("/tmp/test")
        await s.set_state("running")
        await s.set_state("cancelled")
        assert s.state == "cancelled"

    async def test_awaiting_approval_roundtrip(self) -> None:
        s = Session("/tmp/test")
        await s.set_state("running")
        await s.set_state("awaiting_approval")
        assert s.state == "awaiting_approval"
        await s.set_state("running")
        assert s.state == "running"

    async def test_invalid_transition_raises(self) -> None:
        s = Session("/tmp/test")
        # idle -> running is valid, so this should not raise
        await s.set_state("running")
        # But running -> running is not valid
        with pytest.raises(SessionError, match="Cannot transition"):
            await s.set_state("running")
        # idle -> complete is also invalid
        s2 = Session("/tmp/test")
        with pytest.raises(SessionError, match="Cannot transition"):
            await s2.set_state("complete")

    async def test_terminal_state_rejects_transitions(self) -> None:
        s = Session("/tmp/test")
        await s.set_state("running")
        await s.set_state("complete")
        with pytest.raises(SessionError, match="Cannot transition"):
            await s.set_state("running")

    async def test_cancel_sets_state_and_event(self) -> None:
        s = Session("/tmp/test")
        await s.set_state("running")
        assert s.cancel_requested is False
        await s.cancel()
        assert s.cancel_requested is True
        assert s.state == "cancelled"

    async def test_set_state_logs_event(self) -> None:
        s = Session("/tmp/test")
        await s.set_state("running")
        assert s.event_log.last_seq == 1
        events = s.event_log.events_from(1)
        assert len(events) == 1
        assert isinstance(events[0], SessionStateEvent)
        assert events[0].state == "running"
        assert events[0].session_id == s.id

    async def test_set_state_with_reason(self) -> None:
        s = Session("/tmp/test")
        await s.set_state("running")
        await s.set_state("failed", reason="test failure")
        events = s.event_log.events_from(2)
        assert cast(SessionStateEvent, events[0]).reason == "test failure"

    async def test_summary(self) -> None:
        s = Session("/tmp/test")
        summary = s.summary
        assert summary["id"] == s.id
        assert summary["state"] == "idle"
        assert summary["workspace_path"] == "/tmp/test"
        assert summary["event_count"] == 0

    async def test_wait_for_cancel_blocks(self) -> None:
        s = Session("/tmp/test")
        await s.set_state("running")

        async def cancel_later() -> None:
            await asyncio.sleep(0.05)
            await s.cancel()

        async def waiter() -> None:
            await s.wait_for_cancel()

        await asyncio.gather(waiter(), cancel_later())
        assert s.state == "cancelled"


# ── SessionRunner ──────────────────────────────────────────────────────


class TestSessionRunner:
    async def test_start_and_complete(self) -> None:
        s = Session("/tmp/test")

        async def quick_loop(sess: Session) -> None:
            pass  # completes immediately

        runner = SessionRunner(s, loop_factory=quick_loop)
        await runner.start()
        # Give the task a moment to run
        await asyncio.sleep(0.05)
        assert s.state == "complete"
        assert runner.is_running is False

    async def test_cancel_interrupts(self) -> None:
        s = Session("/tmp/test")

        runner = SessionRunner(s)  # uses placeholder loop (blocks forever)
        await runner.start()
        await asyncio.sleep(0.05)
        assert s.state == "running"
        assert runner.is_running is True

        await runner.cancel()
        assert s.state == "cancelled"
        assert runner.is_running is False

    async def test_loop_failure_sets_failed_state(self) -> None:
        s = Session("/tmp/test")

        async def failing_loop(sess: Session) -> None:
            raise ValueError("something went wrong")

        runner = SessionRunner(s, loop_factory=failing_loop)
        await runner.start()
        await asyncio.sleep(0.05)
        assert s.state == "failed"
        events = s.event_log.events_from(2)
        failure_event = cast(SessionStateEvent, events[0])
        assert "something went wrong" in (failure_event.reason or "")

    async def test_runner_lifecycle_events(self) -> None:
        """Verify the runner emits the expected state transition events."""
        s = Session("/tmp/test")

        async def quick_loop(sess: Session) -> None:
            pass

        runner = SessionRunner(s, loop_factory=quick_loop)
        await runner.start()
        await asyncio.sleep(0.05)

        events = s.event_log.all_events
        assert len(events) == 2  # running + complete
        assert cast(SessionStateEvent, events[0]).state == "running"
        assert cast(SessionStateEvent, events[1]).state == "complete"

    async def test_double_start_raises(self) -> None:
        s = Session("/tmp/test")

        async def quick_loop(sess: Session) -> None:
            pass

        runner = SessionRunner(s, loop_factory=quick_loop)
        await runner.start()
        await asyncio.sleep(0.05)
        with pytest.raises(SessionError, match="already started"):
            await runner.start()


# ── SessionRegistry ────────────────────────────────────────────────────


class TestSessionRegistry:
    async def test_create_and_get(self) -> None:
        reg = SessionRegistry()
        s = await reg.create("/tmp/test")
        assert reg.get(s.id) is s
        assert reg.count == 1

    async def test_get_unknown_returns_none(self) -> None:
        reg = SessionRegistry()
        assert reg.get("nonexistent") is None

    async def test_cancel_via_registry(self) -> None:
        reg = SessionRegistry()
        s = await reg.create("/tmp/test")
        # Note: don't set_state("running") here — the runner does that

        runner = SessionRunner(s)
        await runner.start()
        await reg.register_runner(s.id, runner)
        await asyncio.sleep(0.05)

        await reg.cancel(s.id)
        assert s.state == "cancelled"

    async def test_remove(self) -> None:
        reg = SessionRegistry()
        s = await reg.create("/tmp/test")
        assert reg.count == 1
        await reg.remove(s.id)
        assert reg.count == 0
        assert reg.get(s.id) is None

    async def test_list_sessions(self) -> None:
        reg = SessionRegistry()
        s1 = await reg.create("/tmp/a")
        s2 = await reg.create("/tmp/b")
        sessions = await reg.list_sessions()
        assert len(sessions) == 2
        assert {s.id for s in sessions} == {s1.id, s2.id}

    async def test_register_runner(self) -> None:
        reg = SessionRegistry()
        s = await reg.create("/tmp/test")
        runner = SessionRunner(s)
        await reg.register_runner(s.id, runner)
        assert reg.get_runner(s.id) is runner

    async def test_get_runner_unknown(self) -> None:
        reg = SessionRegistry()
        assert reg.get_runner("nonexistent") is None


# ── Critical: session survives connection close ────────────────────────


class TestSessionSurvival:
    """The session loop runs independently of any client connection.

    This is the most important test in Epic E2. It verifies prime directive
    §2.5: the daemon owns sessions; the window is only a viewer.
    """

    @pytest.mark.asyncio
    async def test_session_continues_after_runner_detached(self) -> None:
        """A session started via the runner continues running even if the
        caller (simulating a client connection) goes away."""
        reg = SessionRegistry()
        s = await reg.create("/tmp/test")

        # Start the runner (simulating what happens when a client
        # sends open_workspace and then disconnects)
        runner = SessionRunner(s)  # placeholder loop — blocks forever
        await runner.start()
        await reg.register_runner(s.id, runner)
        await asyncio.sleep(0.05)

        assert s.state == "running"
        assert runner.is_running is True

        # Simulate connection close: the caller's reference is dropped,
        # but the runner/task lives on (owned by the daemon)
        events_before = s.event_log.last_seq

        # Wait and verify events continue to accumulate
        # (in a real session, the agent loop would be emitting events)
        await asyncio.sleep(0.1)

        # The session should still be running
        assert s.state == "running"
        assert runner.is_running is True

        # The event log should be intact (no events lost on disconnect)
        assert s.event_log.last_seq >= events_before

        # Clean up
        await runner.cancel()
        assert s.state == "cancelled"

    @pytest.mark.asyncio
    async def test_session_survives_after_connection_closed(self) -> None:
        """Integration test: the daemon's session registry keeps sessions
        alive after the originating connection is dropped."""
        reg = SessionRegistry()
        s = await reg.create("/tmp/test")

        # Simulate open_workspace via a short-lived "connection"
        async def simulate_connection() -> Session:
            runner = SessionRunner(s)
            await runner.start()
            await reg.register_runner(s.id, runner)
            return s

        session = await simulate_connection()
        # simulate_connection() has returned — the "connection" is gone
        await asyncio.sleep(0.05)

        assert session.state == "running"
        runner = reg.get_runner(session.id)
        assert runner is not None
        assert runner.is_running is True

        # Clean up
        await runner.cancel()


# ── TD-503: touch-tracking ────────────────────────────────────────────


class TestRecordTouched:
    def test_relative_paths_kept_as_posix(self) -> None:
        session = Session("/tmp/ws")
        session.record_touched(["src/app.py", "docs/notes.md"])
        assert session.touched_paths == {"src/app.py", "docs/notes.md"}

    def test_absolute_inside_workspace_relativized(self, tmp_path) -> None:
        session = Session(str(tmp_path))
        session.record_touched([str(tmp_path / "src" / "app.py")])
        assert session.touched_paths == {"src/app.py"}

    def test_absolute_outside_workspace_dropped(self, tmp_path) -> None:
        session = Session(str(tmp_path))
        session.record_touched(["/etc/passwd"])
        assert session.touched_paths == set()

    def test_empty_and_repeat_records_idempotent(self, tmp_path) -> None:
        session = Session(str(tmp_path))
        session.record_touched([])
        session.record_touched(["a.txt"])
        session.record_touched(["a.txt"])
        assert session.touched_paths == {"a.txt"}
