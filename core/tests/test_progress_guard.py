"""Interactive progress guard (TD-5002).

A turn that only reads and runs shell commands pauses. A file write, a
finished answer, or the user pressing Resume lets it continue. Autonomy
sessions are left to the charter breakers.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_cap_enforcement import make_echo_session, wait_for_state
from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tstd.mock import MockProvider, Script
from tstd.progress_guard import (
    IDENTICAL_ROUNDS,
    MAX_AUTO_CONTINUES,
    MODEL_CALL_BUDGET,
    TOOL_WINDOW,
    ProgressGuard,
    interpret_check,
    tool_note,
)
from tstd.protocol import ApprovalRequest, AssistantDelta, TurnComplete
from tstd.protocol import SessionState as SessionStateEvent
from tstd.router import TierRouter
from tstd.session import Session


def test_thresholds_match_the_interactive_contract() -> None:
    assert TOOL_WINDOW == 12
    assert MAX_AUTO_CONTINUES == 3
    assert MODEL_CALL_BUDGET == 52
    assert IDENTICAL_ROUNDS == 3


def _reads(n: int, key: str = "k") -> list[tuple[str, str, bool]]:
    return [("fs_read", json.dumps({key: i}), False) for i in range(n)]


class TestGuardRules:
    def test_twelve_silent_tool_calls_ask_for_a_check(self) -> None:
        guard = ProgressGuard()
        guard.note_tools(_reads(TOOL_WINDOW - 1))
        assert guard.before_model_call() == "proceed"
        guard.note_tools(_reads(1, key="more"))
        assert guard.before_model_call() == "check"

    def test_a_reply_resets_the_tool_window_only(self) -> None:
        guard = ProgressGuard()
        for _ in range(40):
            guard.note_model_call()
        guard.note_tools(_reads(TOOL_WINDOW - 1))
        guard.note_reply()
        guard.note_tools(_reads(1))
        assert guard.tools_since_progress == 1
        assert guard.model_calls_since_progress == 40
        assert guard.before_model_call() == "proceed"

    def test_a_write_resets_the_model_call_budget(self) -> None:
        guard = ProgressGuard()
        for _ in range(40):
            guard.note_model_call()
        guard.note_tools([tool_note("fs_write", {"path": "a.ts"}, status="success")])
        assert guard.model_calls_since_progress == 0
        assert guard.tools_since_progress == 0
        assert guard.before_model_call() == "proceed"

    def test_a_failed_write_is_not_progress(self) -> None:
        guard = ProgressGuard()
        guard.note_tools([tool_note("fs_edit", {"path": "a.ts"}, status="error")])
        assert guard.tools_since_progress == 1

    def test_identical_batches_pause_without_a_check(self) -> None:
        guard = ProgressGuard()
        same = [("shell", '{"command":"ls"}', False)]
        for _ in range(IDENTICAL_ROUNDS - 1):
            guard.note_tools(same)
            assert guard.before_model_call() == "proceed"
        guard.note_tools(same)
        assert guard.before_model_call() == "pause"
        assert "same tool call" in guard.pause_reason

    def test_a_different_command_breaks_the_streak(self) -> None:
        guard = ProgressGuard()
        guard.note_tools([("shell", '{"command":"ls"}', False)])
        guard.note_tools([("shell", '{"command":"ls"}', False)])
        guard.note_tools([("shell", '{"command":"pwd"}', False)])
        assert guard.identical_streak == 1
        assert guard.before_model_call() == "proceed"

    def test_fifty_two_model_calls_without_a_write_pause(self) -> None:
        guard = ProgressGuard()
        for _ in range(MODEL_CALL_BUDGET):
            assert guard.before_model_call() == "proceed"
            guard.note_model_call()
            guard.note_reply()
        assert guard.before_model_call() == "pause"
        assert "52 model calls" in guard.pause_reason

    def test_three_continues_then_the_next_window_pauses(self) -> None:
        guard = ProgressGuard()
        for n in range(MAX_AUTO_CONTINUES):
            guard.note_tools(_reads(TOOL_WINDOW, key=str(n)))
            assert guard.before_model_call() == "check"
            guard.note_auto_continue(f"edit file {n}")
        guard.note_tools(_reads(TOOL_WINDOW, key="last"))
        assert guard.before_model_call() == "pause"
        assert "three checks" in guard.pause_reason

    def test_resume_clears_the_budget(self) -> None:
        guard = ProgressGuard()
        for _ in range(MODEL_CALL_BUDGET):
            guard.note_model_call()
        assert guard.before_model_call() == "pause"
        guard.on_resume()
        assert guard.before_model_call() == "proceed"
        assert guard.model_calls_since_progress == 0


class TestCheckReply:
    def test_done_continue_and_blocked(self) -> None:
        assert interpret_check("DONE: the arrow is in", "") == ("done", "the arrow is in")
        kind, detail = interpret_check("CONTINUE: edit ChatPane", "")
        assert kind == "continue" and detail == "edit ChatPane"
        kind, detail = interpret_check("BLOCKED: icons.ts is missing", "")
        assert kind == "pause" and "icons.ts" in detail

    def test_a_repeated_continue_pauses(self) -> None:
        kind, detail = interpret_check("CONTINUE: edit the pane", "edit the pane")
        assert kind == "pause"
        assert "same next step" in detail

    def test_anything_else_pauses(self) -> None:
        assert interpret_check("", "")[0] == "pause"
        assert interpret_check("I will keep reading", "")[0] == "pause"
        assert interpret_check("DONE:", "")[0] == "pause"


def _echoes(n: int, *tail: Script) -> dict[str, list[Script]]:
    calls = [
        Script(
            kind="tool_call",
            tool_name="echo",
            tool_arguments=json.dumps({"message": f"read-{i}"}),
        )
        for i in range(n)
    ]
    calls.extend(tail)
    return {"test-brain": calls}


def _deltas(session: Session) -> str:
    return "".join(e.delta for e in session.event_log.all_events if isinstance(e, AssistantDelta))


def _pause_reason(session: Session) -> str:
    reasons = [
        e.reason or ""
        for e in session.event_log.all_events
        if isinstance(e, SessionStateEvent) and e.state == "paused"
    ]
    return reasons[-1] if reasons else ""


def _brain_calls(mock: MockProvider) -> list[object]:
    return [call for call in mock.calls if call.model == "test-brain"]


def _request_text(request: object) -> str:
    messages = getattr(request, "messages", [])
    parts: list[str] = []
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            parts.append(content)
    return " ".join(parts)


async def _run(
    tmp_path: Path,
    sequences: dict[str, list[Script]],
    *,
    autonomy: bool = False,
    skip_all: bool = False,
    delegate: bool = False,
):
    session, dispatcher = make_echo_session(tmp_path, session_boundary())
    session.autonomy = autonomy
    session.delegate_depth = 1 if delegate else 0
    if skip_all:
        dispatcher.skip_all_fn = lambda: True
    mock = MockProvider(sequences=sequences)
    # Every tool round counts as a router turn. Stay on the scripted brain
    # model for the whole guard window.
    runner = await start_loop(
        session, TierRouter(lead_turns=80), mock, make_config(), None, dispatcher
    )
    await session.add_user_message("build the feature")
    return session, mock, runner


def session_boundary():
    from tstd.boundary_config import BoundaryConfig

    return BoundaryConfig()


class TestLoop:
    async def test_a_silent_run_pauses_for_the_user(self, tmp_path: Path) -> None:
        session, mock, runner = await _run(
            tmp_path,
            _echoes(TOOL_WINDOW, Script(kind="stream", content="BLOCKED: still reading")),
            skip_all=True,
        )
        await wait_for_state(session, "paused", 8.0)
        reason = _pause_reason(session)
        assert reason.startswith("progress guard:")
        assert "still reading" in reason
        assert "BLOCKED" not in _deltas(session)
        assert any("Progress check" in _request_text(call) for call in _brain_calls(mock))
        assert not [e for e in session.event_log.all_events if isinstance(e, ApprovalRequest)]
        await runner.cancel()

    async def test_done_finishes_the_turn_with_the_answer(self, tmp_path: Path) -> None:
        session, _mock, runner = await _run(
            tmp_path,
            _echoes(TOOL_WINDOW, Script(kind="stream", content="DONE: the arrow is in")),
        )
        turn = await wait_for_turn(session, 1, 8.0)
        assert isinstance(turn, TurnComplete)
        assert turn.failed is False
        assert _deltas(session) == "the arrow is in"
        assert session.state == "running"
        await runner.cancel()

    async def test_continue_opens_another_window(self, tmp_path: Path) -> None:
        session, mock, runner = await _run(
            tmp_path,
            _echoes(
                TOOL_WINDOW,
                Script(kind="stream", content="CONTINUE: edit ChatPane"),
                Script(kind="stream", content="Done"),
            ),
        )
        turn = await wait_for_turn(session, 1, 8.0)
        assert isinstance(turn, TurnComplete)
        assert "Next: edit ChatPane" in _deltas(session)
        assert "Done" in _deltas(session)
        brain = _brain_calls(mock)
        idx = next(i for i, call in enumerate(brain) if "Progress check" in _request_text(call))
        follow = _request_text(brain[idx + 1])
        assert "Progress check" not in follow
        assert "Next: edit ChatPane" in follow
        await runner.cancel()

    async def test_resume_continues_the_same_turn(self, tmp_path: Path) -> None:
        session, _mock, runner = await _run(
            tmp_path,
            _echoes(
                TOOL_WINDOW,
                Script(kind="stream", content="BLOCKED: circling"),
                Script(kind="stream", content="Done after resume"),
            ),
        )
        await wait_for_state(session, "paused", 8.0)
        await session.resume()
        turn = await wait_for_turn(session, 1, 8.0)
        assert isinstance(turn, TurnComplete)
        assert "Done after resume" in _deltas(session)
        assert session.state == "running"
        await runner.cancel()

    async def test_the_same_call_three_times_pauses(self, tmp_path: Path) -> None:
        same = Script(kind="tool_call", tool_name="echo", tool_arguments='{"message": "hi"}')
        session, mock, runner = await _run(
            tmp_path,
            {"test-brain": [same, same, same, Script(kind="stream", content="nope")]},
        )
        await wait_for_state(session, "paused", 8.0)
        assert "same tool call" in _pause_reason(session)
        assert len(_brain_calls(mock)) == IDENTICAL_ROUNDS
        await runner.cancel()

    async def test_autonomy_does_not_take_the_check(self, tmp_path: Path) -> None:
        session, mock, runner = await _run(
            tmp_path,
            _echoes(TOOL_WINDOW, Script(kind="stream", content="Done")),
            autonomy=True,
        )
        turn = await wait_for_turn(session, 1, 8.0)
        assert isinstance(turn, TurnComplete)
        assert _deltas(session) == "Done"
        assert session.state != "paused"
        brain = _brain_calls(mock)
        assert len(brain) == TOOL_WINDOW + 1
        assert all("Progress check" not in _request_text(call) for call in brain)
        await runner.cancel()

    async def test_a_delegate_child_does_not_pause(self, tmp_path: Path) -> None:
        session, mock, runner = await _run(
            tmp_path,
            _echoes(TOOL_WINDOW, Script(kind="stream", content="Done")),
            delegate=True,
        )
        turn = await wait_for_turn(session, 1, 8.0)
        assert isinstance(turn, TurnComplete)
        assert _deltas(session) == "Done"
        assert session.state != "paused"
        brain = _brain_calls(mock)
        assert all("Progress check" not in _request_text(call) for call in brain)
        await runner.cancel()
