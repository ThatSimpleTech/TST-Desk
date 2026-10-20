"""TD-5121: the ``ask_user`` tool parks the turn on questions for the person.

A real daemon runs a scripted turn that calls ``ask_user``; the window's
``answer_question`` goes through the daemon's own message handler. The
turn waits like an approval, the answers come back as the tool result,
and the audit database records both the questions and the answer.
Nothing answers for the person: not skip-all, not an unattended run.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.machine_helpers import eventually
from tests.test_artifacts import _open, _shutdown
from tests.test_two_daemons import _Steps
from tstd import peer_policy
from tstd.audit import AuditStore
from tstd.audit_writer import AuditWriter
from tstd.autonomy.classifier import Boundary, DecisionClassifier
from tstd.daemon import Daemon
from tstd.mock import Script
from tstd.notify import discord, ntfy, slack, telegram
from tstd.protocol import (
    ApprovalRequest,
    QuestionAnswered,
    QuestionRequest,
    SessionState,
    ToolResult,
    TurnComplete,
)
from tstd.session import Session
from tstd.tools.delegate import RESEARCH_CHILD_TOOLS, WORKER_CHILD_TOOLS
from tstd.tools.dispatch import build_decision_request
from tstd.tools.registry import Tool, create_registry

DB = {
    "question": "Which database should the app use?",
    "header": "Database",
    "options": [
        {"label": "Postgres", "description": "Already in docker-compose"},
        {"label": "SQLite"},
    ],
}
FEATURES = {
    "question": "Which features go in first?",
    "options": [{"label": "Auth"}, {"label": "Billing"}, {"label": "Search"}],
    "multi_select": True,
}


def _ask(*questions: dict[str, Any]) -> Script:
    arguments = json.dumps({"questions": list(questions)})
    return Script(kind="tool_call", tool_name="ask_user", tool_arguments=arguments)


@dataclass
class Run:
    daemon: Daemon
    provider: _Steps
    sid: str

    @property
    def session(self) -> Session:
        found = self.daemon.session_registry.get(self.sid)
        assert found is not None
        return found

    def events(self, kind: type[Any]) -> list[Any]:
        return [e for e in self.session.event_log.all_events if isinstance(e, kind)]

    async def start(self, content: str = "set up the app") -> QuestionRequest:
        """Send a message and wait until the turn parks on its question."""
        message = {"type": "user_message", "session_id": self.sid, "content": content}
        await self.daemon._handle_message(json.dumps(message), None)
        await eventually(lambda: bool(self.events(QuestionRequest)), within=20)
        request: QuestionRequest = self.events(QuestionRequest)[-1]
        return request

    async def send(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        raw = await self.daemon._handle_message(json.dumps({"session_id": self.sid, **frame}), None)
        return None if raw is None else dict(json.loads(raw))

    async def answer(self, tool_call_id: str, **body: Any) -> dict[str, Any] | None:
        return await self.send({"type": "answer_question", "tool_call_id": tool_call_id, **body})

    async def finished(self) -> ToolResult:
        await eventually(lambda: bool(self.events(TurnComplete)), within=20)
        [result] = self.events(ToolResult)
        return result

    def tool_reply_seen_by_model(self) -> str:
        last = self.provider.calls[-1]
        tool = [m for m in last.messages if m.role == "tool"]
        assert tool, "the model's next call carried no tool reply"
        content = tool[-1].content
        assert isinstance(content, str)
        return content


async def _run(tmp_path: Path, steps: list[Script]) -> Run:
    provider = _Steps(steps)
    daemon = Daemon(data_dir=tmp_path / "data", provider=provider)
    ws = tmp_path / "ws"
    ws.mkdir()
    sid = await _open(daemon, ws)
    return Run(daemon, provider, sid)


@pytest.fixture
async def asked(tmp_path: Path) -> AsyncIterator[Run]:
    """A turn parked on two questions: one pick, one multi-pick."""
    run = await _run(tmp_path, [_ask(DB, FEATURES), Script(kind="stream", content="Thanks.")])
    try:
        yield run
    finally:
        await _shutdown(run.daemon, run.sid)


# ── The tool ───────────────────────────────────────────────────────────


def test_ask_user_is_a_builtin_the_main_loop_offers_and_children_do_not() -> None:
    tool = create_registry().require("ask_user")
    assert tool.side_effect_class == "auto" and not tool.mutates
    questions = tool.parameters["properties"]["questions"]
    assert (questions["minItems"], questions["maxItems"]) == (1, 4)
    options = questions["items"]["properties"]["options"]
    assert (options["minItems"], options["maxItems"]) == (2, 4)
    assert "ask_user" not in WORKER_CHILD_TOOLS
    assert "ask_user" not in RESEARCH_CHILD_TOOLS


def test_the_classifier_grants_the_builtin_class_a_and_nothing_else() -> None:
    classifier = DecisionClassifier(Boundary(workspace_root=Path("/work/proj")))
    builtin = create_registry().require("ask_user")
    decision = classifier.classify(build_decision_request(builtin, {"questions": [DB]}))
    assert decision.rule is not None and decision.rule.id == "ask-user"
    assert decision.decision_class is not None and decision.decision_class.value == "A"
    impostor = Tool(name="ask_user", provenance="mcp:other")
    other = classifier.classify(build_decision_request(impostor, {"questions": [DB]}))
    assert other.rule is None or other.rule.id != "ask-user"


# ── The turn parks like an approval, and the answer comes back ─────────


async def test_the_turn_parks_on_the_questions_without_an_approval_card(asked: Run) -> None:
    run = asked
    request = await run.start()
    assert [q.question for q in request.questions] == [DB["question"], FEATURES["question"]]
    assert request.questions[1].multi_select is True
    assert run.events(ApprovalRequest) == []
    await eventually(lambda: run.session.state == "awaiting_approval")
    [state] = [e for e in run.events(SessionState) if e.state == "awaiting_approval"]
    assert state.reason == "waiting for your answer"
    assert run.events(TurnComplete) == []
    assert set(run.session.pending_questions) == {request.tool_call_id}
    # Skip-all releases parked approvals, never a question.
    assert run.session.resolve_skippable_approvals() == 0
    assert set(run.session.pending_questions) == {request.tool_call_id}


async def test_the_answers_are_the_tool_result_the_model_reads(asked: Run) -> None:
    run = asked
    request = await run.start()
    reply = await run.answer(
        request.tool_call_id,
        answers=[
            {"selected": ["Postgres"]},
            {"selected": ["Auth", "Billing"], "text": "and audit logs"},
        ],
    )
    assert reply is None
    result = await run.finished()
    assert result.status == "success", result.output
    assert "Answer: Postgres" in result.output
    assert "Answer: Auth, Billing; in their own words: and audit logs" in result.output
    assert run.tool_reply_seen_by_model() == result.output
    [answered] = run.events(QuestionAnswered)
    assert answered.outcome == "answered"
    assert [a.selected for a in answered.answers] == [["Postgres"], ["Auth", "Billing"]]
    assert run.session.pending_questions == {}
    assert run.events(SessionState)[-1].state == "running"


async def test_free_text_alone_answers_a_question(asked: Run) -> None:
    run = asked
    request = await run.start()
    await run.answer(
        request.tool_call_id,
        answers=[{"text": "MySQL, we already run it"}, {"selected": ["Search"]}],
    )
    result = await run.finished()
    assert "Answer: in their own words: MySQL, we already run it" in result.output


@pytest.mark.parametrize(
    ("answers", "problem"),
    [
        ([{"selected": ["Postgres"]}], "Answer all 2 question(s)"),
        ([{"selected": ["MySQL"]}, {"selected": ["Auth"]}], "no option 'MySQL'"),
        ([{"selected": ["Postgres", "SQLite"]}, {"selected": ["Auth"]}], "takes one option"),
        ([{"selected": ["Postgres"]}, {"text": "   "}], "Question 2 has no answer"),
    ],
)
async def test_a_bad_answer_is_refused_and_the_question_stays(
    asked: Run, answers: list[dict[str, Any]], problem: str
) -> None:
    run = asked
    request = await run.start()
    refused = await run.answer(request.tool_call_id, answers=answers)
    assert refused is not None and refused["code"] == "bad_answer"
    assert problem in refused["message"]
    assert refused["in_reply_to"] == "answer_question"
    assert set(run.session.pending_questions) == {request.tool_call_id}
    assert (
        await run.answer(
            request.tool_call_id, answers=[{"selected": ["SQLite"]}, {"selected": ["Auth"]}]
        )
        is None
    )
    assert (await run.finished()).status == "success"


async def test_declining_tells_the_model_to_carry_on(asked: Run) -> None:
    run = asked
    request = await run.start()
    assert await run.answer(request.tool_call_id, declined=True) is None
    result = await run.finished()
    assert result.status == "success"
    assert "chose not to answer" in result.output
    [answered] = run.events(QuestionAnswered)
    assert (answered.outcome, answered.answers) == ("declined", [])


async def test_a_second_answer_finds_nothing_waiting(asked: Run) -> None:
    run = asked
    request = await run.start()
    await run.answer(request.tool_call_id, declined=True)
    await run.finished()
    again = await run.answer(request.tool_call_id, declined=True)
    assert again is not None and again["code"] == "no_pending_question"


async def test_stop_ends_the_wait_and_the_turn(asked: Run) -> None:
    run = asked
    request = await run.start()
    await run.send({"type": "stop_turn"})
    result = await run.finished()
    assert (result.status, result.error_code) == ("error", "question_stopped")
    [answered] = run.events(QuestionAnswered)
    assert answered.outcome == "stopped"
    assert request.tool_call_id not in run.session.pending_questions


async def test_the_approval_timeout_expires_a_question(tmp_path: Path) -> None:
    run = await _run(tmp_path, [_ask(DB), Script(kind="stream", content="ok")])
    try:
        run.session.policy = run.session.policy.model_copy(
            update={"approval_timeout_seconds": 0.05}
        )
        await run.start()
        result = await run.finished()
        assert (result.status, result.error_code) == ("error", "question_timed_out")
        assert run.events(QuestionAnswered)[0].outcome == "timed_out"
    finally:
        await _shutdown(run.daemon, run.sid)


async def test_an_unattended_run_is_refused_before_it_parks(tmp_path: Path) -> None:
    run = await _run(tmp_path, [_ask(DB), Script(kind="stream", content="ok")])
    try:
        run.session.autonomy = True
        message = {"type": "user_message", "session_id": run.sid, "content": "go"}
        await run.daemon._handle_message(json.dumps(message), None)
        result = await run.finished()
        assert (result.status, result.error_code) == ("error", "ask_user_unattended")
        assert run.events(QuestionRequest) == []
    finally:
        await _shutdown(run.daemon, run.sid)


async def test_a_repeated_option_is_refused_without_asking(tmp_path: Path) -> None:
    twice = {"question": "Pick", "options": [{"label": "Yes"}, {"label": " yes "}]}
    run = await _run(tmp_path, [_ask(twice), Script(kind="stream", content="ok")])
    try:
        message = {"type": "user_message", "session_id": run.sid, "content": "go"}
        await run.daemon._handle_message(json.dumps(message), None)
        result = await run.finished()
        assert (result.status, result.error_code) == ("error", "invalid_question")
        assert run.events(QuestionRequest) == []
    finally:
        await _shutdown(run.daemon, run.sid)


# ── The audit database keeps the question and the answer ──────────────


async def test_the_audit_records_the_questions_and_the_answer(asked: Run, tmp_path: Path) -> None:
    run = asked
    store = AuditStore(tmp_path / "audit.db")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(run.session)
    try:
        request = await run.start()
        await run.answer(
            request.tool_call_id,
            answers=[{"selected": ["Postgres"]}, {"selected": ["Auth"], "text": "soon"}],
        )
        await run.finished()
        await writer._queue.join()
        rows = store._conn.execute(
            "SELECT tool_call_id, questions, outcome, answers FROM questions"
        ).fetchall()
    finally:
        await writer.close()
    [(call_id, questions, outcome, answers)] = rows
    assert (call_id, outcome) == (request.tool_call_id, "answered")
    assert [q["question"] for q in json.loads(questions)] == [DB["question"], FEATURES["question"]]
    assert json.loads(answers) == [
        {"selected": ["Postgres"], "text": ""},
        {"selected": ["Auth"], "text": "soon"},
    ]


def test_the_audit_store_scrubs_questions_and_answers(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "audit.db")
    try:
        store.record_session_attach("s1", "/w", 1.0)
        secret = "sk-ant-api03-" + "A" * 40
        store.append_question(
            "s1",
            "tc-1",
            [{"question": f"Use {secret}?", "options": []}],
            "answered",
            [{"selected": [], "text": secret}],
        )
        (questions, answers) = store._conn.execute(
            "SELECT questions, answers FROM questions"
        ).fetchone()
    finally:
        store.close()
    assert secret not in questions and secret not in answers


# ── Phones, other computers and notifications ──────────────────────────


def test_answer_question_is_forwarded_and_never_queued() -> None:
    assert "answer_question" in peer_policy.MACHINE_PEER_VERBS
    assert "answer_question" in peer_policy.FORWARDED_VERBS
    assert "answer_question" in peer_policy.APPROVAL_VERBS


@pytest.mark.parametrize("channel", [ntfy, slack, discord, telegram])
def test_every_notify_channel_says_a_question_is_waiting(channel: Any) -> None:
    from tstd.protocol import Question, QuestionOption

    options = [QuestionOption(label="a"), QuestionOption(label="b")]
    event = QuestionRequest(
        session_id="s1",
        tool_call_id="tc-1",
        questions=[
            Question(question="Which one?", options=options),
            Question(question="And then?", options=options),
        ],
        seq=1,
    )
    assert channel.message_for(event) == "Question: Which one? (+1 more)"
