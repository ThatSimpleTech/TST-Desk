"""Tests for cap enforcement (TD-707).

Covers the acceptance criteria: spend cap halts a session (fault report,
not approval), wall-clock and iteration caps enforce, and resume works
without losing session state.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from tests.test_dispatch import (
    attach_auto_approver,
    make_config,
    start_loop,
    wait_for_turn,
)
from tstd.boundary_config import BoundaryConfig, CapsSection
from tstd.daemon import Daemon
from tstd.loop import _cap_violation
from tstd.mock import MockProvider, Script
from tstd.protocol import ApprovalRequest, TurnComplete
from tstd.protocol import BoundaryUpdate as BoundaryUpdateEvent
from tstd.protocol import SessionState as SessionStateEvent
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import Tool, ToolDispatcher, ToolRegistry


async def wait_for_state(session: Session, state: str, _timeout: float = 3.0) -> None:
    """Wait until the session enters *state*."""
    deadline = time.time() + _timeout
    while time.time() < deadline:
        if session.state == state:
            return
        await asyncio.sleep(0.02)
    raise TimeoutError(f"session did not reach {state!r} (state={session.state!r})")


def make_echo_session(ws: Path, config: BoundaryConfig) -> tuple[Session, ToolDispatcher]:
    """A session with an echo tool + dispatcher and the given boundary."""
    session = Session(str(ws))
    session.boundary_config = config
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="echo",
            description="Echo arguments back",
            parameters={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            side_effect_class="auto",
            parallel_safe=True,
        )
    )
    dispatcher = attach_auto_approver(ToolDispatcher(registry))  # TD-802: mechanics auto-approve

    async def echo_handler(session: object, message: str, tool_call_id: str = "") -> str:
        return f"Echo: {message}"

    dispatcher.register_handler("echo", echo_handler)
    return session, dispatcher


def big_tool_call_sequence() -> dict[str, list[Script]]:
    """A tool-call round trip: big call → tool → follow-up text.

    The first call uses 20k prompt tokens — ~$0.02 under the test config
    (input $1/M, cache-read $0.5/M, output $2/M) — enough to trip a
    $0.01 spend cap on the first call.
    """
    return {
        "test-brain": [
            Script(
                kind="tool_call",
                tool_name="echo",
                tool_arguments='{"message": "hi"}',
                prompt_tokens=20_000,
            ),
            Script(kind="stream", content="Done"),
        ]
    }


# ── AC 5: a $0.01 cap halts on the first call, reported as a fault ─────


class TestSpendCap:
    async def test_spend_cap_halts_session_as_fault_report(self, tmp_path: Path) -> None:
        session, dispatcher = make_echo_session(
            tmp_path,
            BoundaryConfig(caps=CapsSection(spend_usd=0.01)),
        )
        mock = MockProvider(sequences=big_tool_call_sequence())
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), None, dispatcher
        )

        await session.add_user_message("say hi")
        await wait_for_state(session, "paused")

        # Distinct fault state, not an approval request.
        assert session.state == "paused"
        state_events = [
            e
            for e in session.event_log.all_events
            if isinstance(e, SessionStateEvent) and e.state == "paused"
        ]
        assert state_events and "spend cap exceeded" in (state_events[0].reason or "")
        approvals = [e for e in session.event_log.all_events if isinstance(e, ApprovalRequest)]
        assert not approvals  # pause is a fault report, never an approval

        await runner.cancel()


# ── AC 2: wall-clock and iteration caps ─────────────────────────────────


class TestOtherCaps:
    async def test_wall_clock_cap_pauses_before_first_call(self, tmp_path: Path) -> None:
        session, dispatcher = make_echo_session(
            tmp_path,
            BoundaryConfig(caps=CapsSection(wall_clock_hours=0.0)),
        )
        mock = MockProvider(sequences=big_tool_call_sequence())
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), None, dispatcher
        )

        await session.add_user_message("say hi")
        await wait_for_state(session, "paused")

        state_events = [
            e
            for e in session.event_log.all_events
            if isinstance(e, SessionStateEvent) and e.state == "paused"
        ]
        assert state_events and "wall-clock cap exceeded" in (state_events[0].reason or "")
        assert mock.calls == []  # no model call ever happened

        await runner.cancel()

    async def test_iteration_cap_pauses_after_limit(self, tmp_path: Path) -> None:
        session, dispatcher = make_echo_session(
            tmp_path,
            BoundaryConfig(caps=CapsSection(max_iterations=1)),
        )
        mock = MockProvider(sequences=big_tool_call_sequence())
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), None, dispatcher
        )

        await session.add_user_message("say hi")
        await wait_for_state(session, "paused")

        state_events = [
            e
            for e in session.event_log.all_events
            if isinstance(e, SessionStateEvent) and e.state == "paused"
        ]
        assert state_events and "iteration cap exceeded" in (state_events[0].reason or "")

        await runner.cancel()

    async def test_workers_calls_count_toward_the_parents_cap(self, tmp_path: Path) -> None:
        # TD-5195: workers share max_iterations with the parent. Once their
        # calls use it up, the parent pauses at the cap (so the person can
        # raise it) instead of running on while every worker is refused.
        session, dispatcher = make_echo_session(
            tmp_path,
            BoundaryConfig(caps=CapsSection(max_iterations=5)),
        )
        mock = MockProvider(sequences=big_tool_call_sequence())

        # Workers run during the turn (PERF2-07 counts per turn): the tool
        # call stands in for one whose workers used four calls.
        async def busy_echo(session: Session, message: str, tool_call_id: str = "") -> str:
            assert session.delegate_runtime is not None
            session.delegate_runtime.child_calls = 4
            return f"Echo: {message}"

        dispatcher.register_handler("echo", busy_echo)
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), None, dispatcher
        )

        await session.add_user_message("say hi")
        await wait_for_state(session, "paused")

        state_events = [
            e
            for e in session.event_log.all_events
            if isinstance(e, SessionStateEvent) and e.state == "paused"
        ]
        assert state_events and "iteration cap exceeded: 5 >= 5" in (state_events[0].reason or "")

        await runner.cancel()


# ── PERF2-07: an interactive chat's call and clock caps count per turn ──


def _two_turns() -> dict[str, list[Script]]:
    one = big_tool_call_sequence()["test-brain"]
    return {"test-brain": [*one, *one]}


def _pauses(session: Session) -> list[str]:
    return [
        e.reason or ""
        for e in session.event_log.all_events
        if isinstance(e, SessionStateEvent) and e.state == "paused"
    ]


class TestPerTurn:
    async def test_the_call_count_starts_again_each_turn(self, tmp_path: Path) -> None:
        # Two calls a turn under a cap of two: before, the second turn paused
        # at once, because the count ran for the chat's whole life.
        session, dispatcher = make_echo_session(
            tmp_path, BoundaryConfig(caps=CapsSection(max_iterations=2))
        )
        mock = MockProvider(sequences=_two_turns())
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), None, dispatcher
        )
        await session.add_user_message("first")
        await wait_for_turn(session, 1)
        await session.add_user_message("second")
        await wait_for_turn(session, 2)
        assert _pauses(session) == []
        await runner.cancel()

    async def test_the_clock_starts_at_the_turn_not_at_the_app(self, tmp_path: Path) -> None:
        # 0.0003 h is about a second. The loop starts, sits idle for longer,
        # and the first message still runs: before, the clock was uptime.
        session, dispatcher = make_echo_session(
            tmp_path, BoundaryConfig(caps=CapsSection(wall_clock_hours=0.0003))
        )
        mock = MockProvider(sequences=big_tool_call_sequence())
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), None, dispatcher
        )
        await asyncio.sleep(1.3)
        await session.add_user_message("say hi")
        await wait_for_turn(session, 1)
        assert _pauses(session) == []
        await runner.cancel()

    async def test_resume_past_the_call_cap_grants_another_stretch(self, tmp_path: Path) -> None:
        session, dispatcher = make_echo_session(
            tmp_path, BoundaryConfig(caps=CapsSection(max_iterations=1))
        )
        mock = MockProvider(sequences=big_tool_call_sequence())
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), None, dispatcher
        )
        await session.add_user_message("say hi")
        await wait_for_state(session, "paused")
        await session.resume()  # the cap is not raised: Resume alone continues
        turn = await wait_for_turn(session, 1)
        assert isinstance(turn, TurnComplete)
        assert len(_pauses(session)) == 1
        await runner.cancel()

    async def test_resume_past_the_spend_cap_still_needs_it_raised(self, tmp_path: Path) -> None:
        session, dispatcher = make_echo_session(
            tmp_path, BoundaryConfig(caps=CapsSection(spend_usd=0.01))
        )
        mock = MockProvider(sequences=big_tool_call_sequence())
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), None, dispatcher
        )
        await session.add_user_message("say hi")
        await wait_for_state(session, "paused")
        await session.resume()
        await asyncio.sleep(0.2)
        await wait_for_state(session, "paused")
        assert len(_pauses(session)) == 2
        assert all("spend cap" in why for why in _pauses(session))
        await runner.cancel()


# ── AC 4: resume without losing session state ───────────────────────────


class TestResume:
    async def test_resume_after_raising_cap_continues_session(self, tmp_path: Path) -> None:
        session, dispatcher = make_echo_session(
            tmp_path,
            BoundaryConfig(caps=CapsSection(spend_usd=0.01)),
        )
        mock = MockProvider(sequences=big_tool_call_sequence())
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), None, dispatcher
        )

        await session.add_user_message("say hi")
        await wait_for_state(session, "paused")

        # User raises the cap and resumes — the parked loop re-checks and
        # continues the same turn (no state loss).
        session.boundary_config = BoundaryConfig(caps=CapsSection(spend_usd=100.0))
        await session.resume()

        turn = await wait_for_turn(session, 1)
        assert isinstance(turn, TurnComplete)
        assert session.state == "running"

        # The tool result from the paused turn is preserved in the log.
        from tstd.protocol import ToolResult as ToolResultEvent

        results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
        assert results and "Echo: hi" in results[0].output

        await runner.cancel()


class _Cost:
    def session_cost(self) -> float:
        return 999.0


def test_skip_all_does_not_pause_at_cap(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    session.boundary_config = BoundaryConfig(caps=CapsSection(spend_usd=0.01, max_iterations=1))
    tracker = _Cost()
    assert _cap_violation(session, tracker, time.time(), 10_000, skip_all=True) is None
    assert _cap_violation(session, tracker, time.time(), 1, skip_all=False) is not None


def test_skip_all_does_not_waive_autonomy_caps(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    session.autonomy = True
    session.boundary_config = BoundaryConfig(caps=CapsSection(spend_usd=0.01, max_iterations=1))
    tracker = _Cost()
    reason = _cap_violation(session, tracker, time.time(), 10_000, skip_all=True)
    assert reason is not None
    assert reason.startswith("spend cap")


# ── Daemon resume handler ───────────────────────────────────────────────


class TestDaemonResume:
    async def test_resume_message_reloads_boundary_and_resumes(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = json.dumps({"type": "open_workspace", "path": str(tmp_path)})
        response = await daemon._handle_message(raw, None)
        assert response is not None
        sid = json.loads(response)["session_id"]
        session = daemon.session_registry.get(sid)
        assert session is not None

        await session.pause_at_cap("spend cap exceeded (test)")
        assert session.state == "paused"

        await daemon._handle_message(json.dumps({"type": "resume", "session_id": sid}), None)
        assert session.state == "running"

        updates = [e for e in session.event_log.all_events if isinstance(e, BoundaryUpdateEvent)]
        assert updates and updates[-1].source == "resume"

        runner = daemon.session_registry.get_runner(sid)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()
