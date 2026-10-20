"""Questions the agent asks the person with ``ask_user`` (TD-5121).

A question parks its turn the way an approval does: the session goes to
``awaiting_approval``, the ``question_request`` event puts a card on every
attached window and phone, and the call waits on a session-owned future
(no viewer owns it, prime directive §2.5) until ``answer_question``,
Stop, cancel, or the approval timeout. ``question_answered`` then logs how
it ended with the answers, before the call's ``tool_result``.

Nothing here answers for the person: skip-all releases approvals, never
questions, and an unattended run is refused before it parks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .protocol import (
    AnswerQuestion,
    Question,
    QuestionAnswer,
    QuestionAnswered,
    QuestionOutcomeKind,
    QuestionRequest,
    build_error,
)

if TYPE_CHECKING:
    from .session import Session

WAITING_REASON = "waiting for your answer"


@dataclass(frozen=True)
class QuestionReply:
    """How a parked question was resolved, and the answers when it was."""

    outcome: QuestionOutcomeKind
    answers: tuple[QuestionAnswer, ...] = ()


@dataclass
class PendingQuestion:
    """One ``ask_user`` call waiting for the person."""

    future: asyncio.Future[QuestionReply]
    questions: tuple[Question, ...]


async def ask(session: Session, tool_call_id: str, questions: Sequence[Question]) -> QuestionReply:
    """Park *questions* on *session* and wait for the person.

    Logs ``question_request``, then ``question_answered`` however it ends.
    With ``policy.approval_timeout_seconds`` set, the wait expires as
    ``timed_out``, the same budget an approval gets.
    """
    fut: asyncio.Future[QuestionReply] = asyncio.get_running_loop().create_future()
    # Stop or cancel may land before the question parks; it ends at once.
    if session.cancel_requested:
        fut.set_result(QuestionReply("cancelled"))
    elif session.stop_turn_requested:
        fut.set_result(QuestionReply("stopped"))
    session.pending_questions[tool_call_id] = PendingQuestion(fut, tuple(questions))
    try:
        await session.event_log.add(
            QuestionRequest(
                session_id=session.id,
                tool_call_id=tool_call_id,
                questions=list(questions),
                seq=1,
            )
        )
        await session.enter_awaiting(WAITING_REASON)
        reply = await _wait(fut, session.policy.approval_timeout_seconds)
    finally:
        session.pending_questions.pop(tool_call_id, None)
        await session.leave_awaiting("question resolved")
    await session.event_log.add(
        QuestionAnswered(
            session_id=session.id,
            tool_call_id=tool_call_id,
            outcome=reply.outcome,
            answers=list(reply.answers),
            seq=1,
        )
    )
    return reply


async def _wait(fut: asyncio.Future[QuestionReply], limit: float | None) -> QuestionReply:
    if limit is None:
        return await fut
    try:
        # Shielded so expiry leaves the future alone: a late answer then
        # finds it gone from the map and is refused, never half-applied.
        return await asyncio.wait_for(asyncio.shield(fut), limit)
    except TimeoutError:
        return QuestionReply("timed_out")


def question_text(event: QuestionRequest) -> str:
    """One line for a notification: the first question, and how many follow."""
    first = event.questions[0].question
    more = len(event.questions) - 1
    return f"{first} (+{more} more)" if more else first


def resolve_all(session: Session, outcome: QuestionOutcomeKind) -> None:
    """End every parked question with *outcome* (Stop, cancel)."""
    for pending in session.pending_questions.values():
        if not pending.future.done():
            pending.future.set_result(QuestionReply(outcome))


def check_answers(questions: Sequence[Question], answers: Sequence[QuestionAnswer]) -> str | None:
    """Why *answers* cannot answer *questions*, or ``None`` when they can."""
    if len(answers) != len(questions):
        return f"Answer all {len(questions)} question(s), in order, or decline them."
    for number, (question, answer) in enumerate(zip(questions, answers, strict=True), 1):
        labels = [option.label for option in question.options]
        unknown = [label for label in answer.selected if label not in labels]
        if unknown:
            return f"Question {number} has no option {unknown[0]!r}."
        if len(set(answer.selected)) != len(answer.selected):
            return f"Question {number} lists an option twice."
        if not question.multi_select and len(answer.selected) > 1:
            return f"Question {number} takes one option."
        if not answer.selected and not answer.text.strip():
            return f"Question {number} has no answer: pick an option or type one."
    return None


def answer(session: Session, msg: AnswerQuestion) -> str | None:
    """Apply the person's ``answer_question``. An error frame, or ``None``."""
    pending = session.pending_questions.get(msg.tool_call_id)
    if pending is None or pending.future.done():
        return build_error(
            "no_pending_question",
            f"No question {msg.tool_call_id!r} is waiting in session {msg.session_id!r}",
            session_id=msg.session_id,
            in_reply_to=msg.type,
        )
    if msg.declined:
        pending.future.set_result(QuestionReply("declined"))
        return None
    problem = check_answers(pending.questions, msg.answers)
    if problem is not None:
        return build_error("bad_answer", problem, session_id=msg.session_id, in_reply_to=msg.type)
    answers = tuple(
        QuestionAnswer(selected=list(a.selected), text=a.text.strip()) for a in msg.answers
    )
    pending.future.set_result(QuestionReply("answered", answers))
    return None


def _answer_line(answer: QuestionAnswer) -> str:
    parts = [", ".join(answer.selected)] if answer.selected else []
    if answer.text:
        parts.append(f"in their own words: {answer.text}")
    return "; ".join(parts)


def format_reply(questions: Sequence[Question], reply: QuestionReply) -> str:
    """The tool result text the model reads for an answered or declined call."""
    if reply.outcome == "declined":
        return (
            "The user chose not to answer. Don't ask the same thing again this turn: "
            "carry on with your best judgment and say what you assumed."
        )
    lines = ["The user answered:"]
    for number, (question, answer) in enumerate(zip(questions, reply.answers, strict=True), 1):
        lines.append(f"{number}. {question.question}")
        lines.append(f"   Answer: {_answer_line(answer)}")
    return "\n".join(lines)


UNANSWERED: dict[QuestionOutcomeKind, tuple[str, str]] = {
    "stopped": ("question_stopped", "The user stopped the turn before answering."),
    "cancelled": ("question_cancelled", "The session ended before the user answered."),
    "timed_out": (
        "question_timed_out",
        "No answer came before the approval timeout. Carry on with your best judgment "
        "and say what you assumed.",
    ),
}
