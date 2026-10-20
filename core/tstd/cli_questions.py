"""TTY answers to ``ask_user`` questions for ``tst run`` / ``tst attach`` (TD-5121).

A question is asked the way an approval is (TD-3103, TD-5111): on a TTY
the person answers each question here and the CLI sends one
``answer_question``; without a TTY nothing is read, and the question waits
for the window. A run that may not wait for a person (``--on-ask
deny-and-continue``, TD-5118) never sees a question: the daemon's
``ask_user`` refuses in that turn, as approvals are refused, and the
model carries on.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, TextIO

NON_TTY_COPY = "Answer it in the TST Desk window, or run this on a TTY."


def _questions(event: dict[str, Any]) -> list[dict[str, Any]]:
    raw = event.get("questions")
    return [q for q in raw if isinstance(q, dict)] if isinstance(raw, list) else []


def _labels(question: dict[str, Any]) -> list[str]:
    options = question.get("options")
    if not isinstance(options, list):
        return []
    return [str(o.get("label") or "") for o in options if isinstance(o, dict)]


def format_question(question: dict[str, Any], number: int, total: int) -> str:
    """One question's lines: its text, then each option numbered."""
    head = str(question.get("header") or "").strip()
    prefix = f"question {number}/{total}" + (f" [{head}]" if head else "")
    lines = [f"{prefix}: {question.get('question') or ''}"]
    raw = question.get("options")
    options: list[Any] = raw if isinstance(raw, list) else []
    for index, option in enumerate(options, 1):
        if not isinstance(option, dict):
            continue
        detail = str(option.get("description") or "").strip()
        lines.append(f"  {index}. {option.get('label')}" + (f" - {detail}" if detail else ""))
    many = "numbers, comma-separated" if question.get("multi_select") else "a number"
    lines.append(f"  ({many}, or type your own answer)")
    return "\n".join(lines)


def parse_answer(question: dict[str, Any], raw: str) -> dict[str, Any] | None:
    """One line as a ``QuestionAnswer``, or ``None`` to ask again.

    Option numbers pick options; anything else is the person's own words.
    A blank line is no answer.
    """
    text = raw.strip()
    if not text:
        return None
    labels = _labels(question)
    tokens = [t for t in text.replace(",", " ").split() if t]
    if tokens and all(t.isdigit() for t in tokens):
        picks = [int(t) for t in tokens]
        if any(p < 1 or p > len(labels) for p in picks) or len(set(picks)) != len(picks):
            return None
        if len(picks) > 1 and not question.get("multi_select"):
            return None
        return {"selected": [labels[p - 1] for p in picks], "text": ""}
    return {"selected": [], "text": text}


def answer_message(
    event: dict[str, Any], session_id: str, answers: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "type": "answer_question",
        "session_id": session_id,
        "tool_call_id": str(event.get("tool_call_id") or ""),
        "answers": answers,
    }


def summary(event: dict[str, Any]) -> str:
    """The first question, for one-line notices."""
    questions = _questions(event)
    return str(questions[0].get("question") or "a question") if questions else "a question"


def _isatty(stdin: Any) -> bool:
    check = getattr(stdin, "isatty", None)
    return bool(check()) if callable(check) else False


def _emit(stream: TextIO, text: str) -> None:
    print(text, file=stream, flush=True)


async def prompt_questions(
    event: dict[str, Any],
    session_id: str,
    *,
    stdin: Any | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> dict[str, Any] | None:
    """Ask each question on a TTY and return the ``answer_question`` to send.

    ``None`` without a TTY (nothing read, nothing sent), or when stdin
    ends before every question is answered: the question stays parked.
    """
    in_stream: Any = sys.stdin if stdin is None else stdin
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    questions = _questions(event)
    if not _isatty(in_stream):
        _emit(out, f"question: {summary(event)}")
        _emit(err, NON_TTY_COPY)
        return None
    answers: list[dict[str, Any]] = []
    for number, question in enumerate(questions, 1):
        _emit(out, format_question(question, number, len(questions)))
        while True:
            line = await asyncio.to_thread(in_stream.readline)
            if line == "":
                return None
            parsed = parse_answer(question, line)
            if parsed is not None:
                answers.append(parsed)
                break
            _emit(out, "  ? pick a listed number, or type an answer")
    return answer_message(event, session_id, answers)
