"""Circuit breakers (TD-4203, spec §12.7).

Each breaker has a test that trips it. A trip is a fault report — the
run stops and notifies — never an ApprovalRequest. Interactive sessions
never trip. Spend/wall-clock caps and Class C keep their existing
reason strings.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_autonomy_loop import _wire_autonomy, make_charter
from tests.test_cap_enforcement import (
    big_tool_call_sequence,
    make_echo_session,
    wait_for_state,
)
from tests.test_dispatch import make_config, start_loop
from tests.test_loop import start_loop as start_text_loop
from tstd.autonomy.breakers import (
    FILE_THRASH,
    NO_DOD_PROGRESS,
    TESTS_RED,
    TOOL_LOOP,
    limits_for,
    maybe_trip,
    record_file_write,
    record_tool_round,
)
from tstd.autonomy.dod import DodItemResult, DodPoll
from tstd.autonomy.runner import CLASS_C_STOP, advance_autonomy, first_prompt, should_notify
from tstd.boundary_config import BoundaryConfig, CapsSection
from tstd.mock import MockProvider, Script
from tstd.protocol import ApprovalRequest
from tstd.router import TierRouter
from tstd.session import Session


def _no_approvals(session: Session) -> None:
    assert not any(isinstance(e, ApprovalRequest) for e in session.event_log.all_events)


def _red_poll() -> DodPoll:
    return DodPoll((DodItemResult("The suite is green", False, "RED", "worker"),))


def _stalled_poll() -> DodPoll:
    return DodPoll(
        (
            DodItemResult("one item", True, "GREEN", "worker"),
            DodItemResult("another", False, "RED", "worker"),
        )
    )


def _autonomy_session(tmp_path: Path, *, max_iterations: int = 20) -> tuple[Session, list[str]]:
    session = Session(str(tmp_path))
    notified = _wire_autonomy(session, make_charter(max_iterations=max_iterations))
    return session, notified


# ── Thresholds ───────────────────────────────────────────────────────────


class TestLimits:
    def test_defaults_match_charter_examples(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.charter = make_charter()
        limits = limits_for(session)
        assert limits.tests_red == 3
        assert limits.file_thrash == 5
        assert limits.no_dod_progress == 3
        assert limits.tool_loop == 3

    def test_charter_stop_conditions_override_n(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.charter = make_charter(max_iterations=20).model_copy(
            update={
                "stop_conditions": [
                    "tests red for 2 consecutive iterations",
                    "same file modified 4+ times without test improvement",
                    "no measurable progress against definition-of-done for 6 iterations",
                    "identical tool-call loop for 2 rounds",
                ]
            }
        )
        limits = limits_for(session)
        assert limits.tests_red == 2
        assert limits.file_thrash == 4
        assert limits.no_dod_progress == 6
        assert limits.tool_loop == 2


# ── New breakers ─────────────────────────────────────────────────────────


class TestTestsRed:
    async def test_trips_after_n_all_red_polls(self, tmp_path: Path) -> None:
        session, notified = _autonomy_session(tmp_path)

        async def _red() -> DodPoll:
            return _red_poll()

        session.dod_poller = _red
        assert await advance_autonomy(session) is True
        assert await advance_autonomy(session) is True
        assert session.autonomy_stop_reason is None
        assert await advance_autonomy(session) is False
        assert session.autonomy_stop_reason == TESTS_RED
        assert any(TESTS_RED in n for n in notified)
        _no_approvals(session)


class TestNoDodProgress:
    async def test_trips_when_greens_do_not_improve(self, tmp_path: Path) -> None:
        session, notified = _autonomy_session(tmp_path)

        async def _stalled() -> DodPoll:
            return _stalled_poll()

        session.dod_poller = _stalled
        assert await advance_autonomy(session) is True
        assert await advance_autonomy(session) is True
        assert await advance_autonomy(session) is False
        assert session.autonomy_stop_reason == NO_DOD_PROGRESS
        assert any(NO_DOD_PROGRESS in n for n in notified)
        _no_approvals(session)


class TestFileThrash:
    async def test_trips_after_n_writes_without_improvement(self, tmp_path: Path) -> None:
        session, notified = _autonomy_session(tmp_path)
        for _ in range(5):
            record_file_write(session, "src/main.py")
        reason = maybe_trip(session)
        assert reason == FILE_THRASH
        session.autonomy_stop_reason = reason
        assert await advance_autonomy(session) is False
        assert any(FILE_THRASH in n for n in notified)
        _no_approvals(session)
        assert should_notify(FILE_THRASH)

    def test_improvement_clears_the_count(self, tmp_path: Path) -> None:
        session, _notified = _autonomy_session(tmp_path)
        for _ in range(5):
            record_file_write(session, "src/main.py")
        session.last_dod_green_count = 0
        session.autonomy_turns = 1
        session.last_dod_poll = _stalled_poll()
        assert maybe_trip(session) is None
        assert session.file_write_counts == {}


class TestToolLoop:
    async def test_trips_after_n_identical_rounds(self, tmp_path: Path) -> None:
        session, notified = _autonomy_session(tmp_path)
        items = (("echo", {"message": "hi"}),)
        record_tool_round(session, items)
        record_tool_round(session, items)
        assert maybe_trip(session) is None
        record_tool_round(session, items)
        reason = maybe_trip(session)
        assert reason == TOOL_LOOP
        session.autonomy_stop_reason = reason
        assert await advance_autonomy(session) is False
        assert any(TOOL_LOOP in n for n in notified)
        _no_approvals(session)
        assert should_notify(TOOL_LOOP)

    async def test_loop_records_fingerprint_and_trips(self, tmp_path: Path) -> None:
        charter = make_charter(max_iterations=20)
        session, dispatcher = make_echo_session(
            tmp_path,
            BoundaryConfig(boundary=charter.boundary, caps=charter.caps),
        )
        notified = _wire_autonomy(session, charter)
        dispatcher.autonomy_fn = lambda: session.autonomy
        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="echo",
                        tool_arguments='{"message": "hi"}',
                    ),
                    Script(
                        kind="tool_call",
                        tool_name="echo",
                        tool_arguments='{"message": "hi"}',
                    ),
                    Script(
                        kind="tool_call",
                        tool_name="echo",
                        tool_arguments='{"message": "hi"}',
                    ),
                    Script(kind="stream", content="Done"),
                ],
                "test-worker": [Script(kind="text", content="RED")],
            }
        )
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), None, dispatcher
        )
        await session.add_user_message(first_prompt(charter))
        await wait_for_state(session, "complete")
        assert session.autonomy_stop_reason == TOOL_LOOP
        assert any(TOOL_LOOP in n for n in notified)
        _no_approvals(session)
        assert not runner.is_running


# ── Existing stops stay faults, not cards ────────────────────────────────


class TestExistingStops:
    async def test_class_c_still_stops_and_is_not_a_breaker(self, tmp_path: Path) -> None:
        session, notified = _autonomy_session(tmp_path)
        session.autonomy_class_c = True
        session.red_dod_streak = 99
        session.tool_loop_streak = 99
        assert await advance_autonomy(session) is False
        assert session.autonomy_stop_reason == CLASS_C_STOP
        assert not CLASS_C_STOP.startswith("breaker:")
        assert any(CLASS_C_STOP in n for n in notified)
        _no_approvals(session)

    async def test_spend_cap_still_notifies(self, tmp_path: Path) -> None:
        charter = make_charter(spend_usd=0.01, max_iterations=40)
        session, dispatcher = make_echo_session(
            tmp_path, BoundaryConfig(caps=CapsSection(spend_usd=0.01, max_iterations=40))
        )
        notified = _wire_autonomy(session, charter)
        dispatcher.skip_all_fn = lambda: True
        dispatcher.autonomy_fn = lambda: session.autonomy
        mock = MockProvider(sequences=big_tool_call_sequence())
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), None, dispatcher
        )
        await session.add_user_message(first_prompt(charter))
        await wait_for_state(session, "complete")
        assert session.autonomy_stop_reason is not None
        assert session.autonomy_stop_reason.startswith("spend cap")
        assert any("spend cap" in n for n in notified)
        _no_approvals(session)
        assert not runner.is_running
        assert should_notify(session.autonomy_stop_reason)

    async def test_wall_clock_cap_still_notifies(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        charter = make_charter(wall_clock_hours=0.0, max_iterations=40)
        notified = _wire_autonomy(session, charter)
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="Working")})
        runner = await start_text_loop(session, TierRouter(), mock, make_config())
        await session.add_user_message(first_prompt(charter))
        await wait_for_state(session, "complete")
        assert session.autonomy_stop_reason is not None
        assert session.autonomy_stop_reason.startswith("wall-clock cap")
        assert any("wall-clock cap" in n for n in notified)
        _no_approvals(session)
        assert not runner.is_running
        assert should_notify(session.autonomy_stop_reason)


# ── Interactive / not a card ─────────────────────────────────────────────


class TestInteractiveNeverTrips:
    def test_high_counters_do_not_trip(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.autonomy = False
        session.charter = make_charter(max_iterations=20)
        session.red_dod_streak = 99
        session.no_dod_progress_streak = 99
        session.tool_loop_streak = 99
        session.file_write_counts["src/main.py"] = 99
        record_tool_round(session, (("echo", {"message": "hi"}),))
        record_file_write(session, "src/main.py")
        assert session.tool_loop_streak == 99
        assert session.file_write_counts["src/main.py"] == 99
        assert maybe_trip(session) is None
        assert session.autonomy_stop_reason is None
        _no_approvals(session)
