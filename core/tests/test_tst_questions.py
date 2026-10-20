"""``ask_user`` questions in ``tst run`` and ``tst attach`` (TD-5121).

A question is handled the way an approval is (TD-3103, TD-5111): a TTY
answers it, and without one the run ends with ``approval_needed`` while
the question waits for the window.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_tst_approvals import _Stdin, _Ws
from tests.test_tst_run_scripts import (
    _json_line,
    _only_session_id,
    _start,
    _stop,
    _tool_turn,
    _workspace,
)
from tstd import cli, cli_run, cli_turn
from tstd.cli_questions import (
    NON_TTY_COPY,
    format_question,
    parse_answer,
    prompt_questions,
)
from tstd.cli_turn import EXIT_APPROVAL_NEEDED, EXIT_OK
from tstd.protocol import QuestionAnswered, ToolResult

_SESSION = "sess-1"
_PICK: dict[str, Any] = {
    "question": "Which database?",
    "header": "Database",
    "options": [{"label": "Postgres", "description": "in compose"}, {"label": "SQLite"}],
}
_MANY: dict[str, Any] = {
    "question": "Which features?",
    "options": [{"label": "Auth"}, {"label": "Billing"}, {"label": "Search"}],
    "multi_select": True,
}
_QUESTION: dict[str, Any] = {
    "type": "question_request",
    "session_id": _SESSION,
    "tool_call_id": "tc-q",
    "questions": [_PICK, _MANY],
}
_TURN_DONE: dict[str, Any] = {"type": "turn_complete", "session_id": _SESSION, "failed": False}


class TestParse:
    def test_numbers_pick_options_and_words_are_free_text(self) -> None:
        assert parse_answer(_PICK, " 2 \n") == {"selected": ["SQLite"], "text": ""}
        assert parse_answer(_MANY, "1, 3") == {"selected": ["Auth", "Search"], "text": ""}
        assert parse_answer(_PICK, "MySQL please") == {"selected": [], "text": "MySQL please"}

    @pytest.mark.parametrize("line", ["", "   \n", "3", "0", "1 1"])
    def test_out_of_range_repeated_or_blank_asks_again(self, line: str) -> None:
        assert parse_answer(_PICK, line) is None

    def test_one_pick_only_unless_multi_select(self) -> None:
        assert parse_answer(_PICK, "1 2") is None
        assert parse_answer(_MANY, "1 2") == {"selected": ["Auth", "Billing"], "text": ""}

    def test_the_card_numbers_each_option(self) -> None:
        card = format_question(_PICK, 1, 2)
        assert card.splitlines()[0] == "question 1/2 [Database]: Which database?"
        assert "  1. Postgres - in compose" in card
        assert "  2. SQLite" in card


class TestPrompt:
    async def test_a_tty_answers_each_question_in_turn(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        reply = await prompt_questions(
            _QUESTION, _SESSION, stdin=_Stdin("9\n1\n2,3\n", tty=True), stdout=out, stderr=err
        )
        assert reply == {
            "type": "answer_question",
            "session_id": _SESSION,
            "tool_call_id": "tc-q",
            "answers": [
                {"selected": ["Postgres"], "text": ""},
                {"selected": ["Billing", "Search"], "text": ""},
            ],
        }
        assert "pick a listed number" in out.getvalue()

    async def test_without_a_tty_nothing_is_read(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        stdin = _Stdin("1\n", tty=False)
        reply = await prompt_questions(_QUESTION, _SESSION, stdin=stdin, stdout=out, stderr=err)
        assert reply is None
        assert NON_TTY_COPY in err.getvalue()
        assert stdin.readline() == "1\n"

    async def test_stdin_ending_early_leaves_it_for_the_window(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        reply = await prompt_questions(
            _QUESTION, _SESSION, stdin=_Stdin("1\n", tty=True), stdout=out, stderr=err
        )
        assert reply is None


class TestStreamWiring:
    async def test_run_on_a_tty_sends_the_answers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli.sys, "stdin", _Stdin("1\nship auth first\n", tty=True))
        ws = _Ws([_QUESTION, _TURN_DONE])
        outcome = await cli_turn.stream_turn(ws, _SESSION)
        assert outcome.exit_code == EXIT_OK
        [sent] = ws.sent
        assert sent["type"] == "answer_question"
        assert sent["answers"][1] == {"selected": [], "text": "ship auth first"}

    @pytest.mark.parametrize("output", ["json", "ndjson"])
    async def test_machine_modes_never_prompt(
        self, monkeypatch: pytest.MonkeyPatch, output: str
    ) -> None:
        monkeypatch.setattr(cli.sys, "stdin", _Stdin("1\n1\n", tty=True))
        ws = _Ws([_QUESTION, _TURN_DONE])
        outcome = await cli_turn.stream_turn(ws, _SESSION, output=output)  # type: ignore[arg-type]
        assert outcome.exit_code == EXIT_APPROVAL_NEEDED
        assert "Which database?" in (outcome.error_message or "")
        assert ws.sent == []

    async def test_attach_on_a_tty_answers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli.sys, "stdin", _Stdin("2\n1\n", tty=True))
        ws = _Ws([_QUESTION])
        assert await cli._stream_attached(ws, _SESSION) == 0
        [sent] = ws.sent
        assert [a["selected"] for a in sent["answers"]] == [["SQLite"], ["Auth"]]


# ── Against a real daemon ───────────────────────────────────────────────


async def test_tst_run_on_a_tty_answers_and_the_model_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", _Stdin("2\n", tty=True))
    data_dir = tmp_path / "data"
    workspace = _workspace(tmp_path)
    provider = _tool_turn("ask_user", {"questions": [_PICK]})
    daemon, task = await _start(data_dir, provider)
    try:
        code = await cli_run.run_turn(workspace, "set it up", data_dir)
        sid = await _only_session_id(daemon)
        session = daemon.session_registry.get(sid)
        assert session is not None
        events = session.event_log.all_events
    finally:
        await _stop(daemon, task)
    assert code == EXIT_OK
    [answered] = [e for e in events if isinstance(e, QuestionAnswered)]
    assert answered.answers[0].selected == ["SQLite"]
    [result] = [e for e in events if isinstance(e, ToolResult)]
    assert "Answer: SQLite" in result.output


@pytest.mark.parametrize("output", ["text", "json"])
async def test_an_unanswerable_question_exits_approval_needed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output: str,
) -> None:
    monkeypatch.setattr(cli_run.sys, "stdin", _Stdin("1\n", tty=False))
    data_dir = tmp_path / "data"
    workspace = _workspace(tmp_path)
    provider = _tool_turn("ask_user", {"questions": [_PICK]})
    daemon, task = await _start(data_dir, provider)
    try:
        code = await cli_run.run_turn(
            workspace,
            "set it up",
            data_dir,
            output=output,  # type: ignore[arg-type]
        )
        sid = await _only_session_id(daemon)
        session = daemon.session_registry.get(sid)
        assert session is not None
        for _ in range(100):
            if session.state == "awaiting_approval":
                break
            await asyncio.sleep(0.02)
        state = session.state
        waiting = set(session.pending_questions)
    finally:
        await _stop(daemon, task)
    assert code == EXIT_APPROVAL_NEEDED
    # The question waits for the window; nothing answered it.
    assert state == "awaiting_approval"
    assert len(waiting) == 1
    if output == "json":
        result = _json_line(capsys.readouterr().out)
        assert result["status"] == "approval_needed"
        assert json.dumps(result).count("Which database?") == 1
