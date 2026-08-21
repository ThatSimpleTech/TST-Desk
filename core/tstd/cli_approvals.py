"""TTY approval cards for ``tst run`` / ``tst attach`` (TD-3103).

Reuses the daemon's ``approve`` / ``deny`` / ``always_allow`` messages.
Class C (or a missing ``proposed_always_allow``) never sends ``always_allow``.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, TextIO

CLASS_C_NOT_ALWAYS_ALLOWABLE = "class_c_not_always_allowable"
CLASS_C_NOT_ALWAYS_ALLOWABLE_MESSAGE = "Class C actions can never be always-allowed"
NON_TTY_COPY = "Use the window or a TTY to approve this call."


def always_allowable(event: dict[str, Any]) -> bool:
    """False for Class C, or when the daemon omitted a proposed rule."""
    if event.get("decision_class") == "C":
        return False
    return event.get("proposed_always_allow") is not None


def format_approval_card(event: dict[str, Any]) -> str:
    """Card lines: summary, tool, class."""
    summary = str(event.get("summary") or event.get("tool_name") or "approval")
    tool = str(event.get("tool_name") or "tool")
    decision_class = str(event.get("decision_class") or "?")
    return f"approval: {summary}\n  tool: {tool}\n  class: {decision_class}"


def parse_approval_answer(
    event: dict[str, Any], session_id: str, raw: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Map one line to a client message.

    Returns ``(message, None)`` to send, ``(None, code)`` to refuse and
    re-prompt, or ``(None, None)`` when the line is not ``y`` / ``n`` /
    ``always``.
    """
    token = raw.strip().lower()
    tool_call_id = str(event.get("tool_call_id") or "")
    if token == "y":
        return (
            {"type": "approve", "session_id": session_id, "tool_call_id": tool_call_id},
            None,
        )
    if token == "n":
        return (
            {"type": "deny", "session_id": session_id, "tool_call_id": tool_call_id},
            None,
        )
    if token == "always":
        if not always_allowable(event):
            return None, CLASS_C_NOT_ALWAYS_ALLOWABLE
        return (
            {
                "type": "always_allow",
                "session_id": session_id,
                "tool_call_id": tool_call_id,
            },
            None,
        )
    return None, None


def _isatty(stdin: Any) -> bool:
    check = getattr(stdin, "isatty", None)
    return bool(check()) if callable(check) else False


def _emit(stream: TextIO, text: str) -> None:
    print(text, file=stream, flush=True)


async def prompt_approval(
    event: dict[str, Any],
    session_id: str,
    *,
    stdin: Any | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> dict[str, Any] | None:
    """Print the card. On a TTY, read ``y`` / ``n`` / ``always``.

    Non-TTY: print copy to use the window and return ``None`` without
    sending. Does not block on stdin when it is not a TTY.
    """
    in_stream: Any = sys.stdin if stdin is None else stdin
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    _emit(out, format_approval_card(event))
    if not _isatty(in_stream):
        _emit(err, NON_TTY_COPY)
        return None

    offered = "y / n / always" if always_allowable(event) else "y / n"
    _emit(out, offered)
    while True:
        line = await asyncio.to_thread(in_stream.readline)
        if line == "":
            return None
        message, refuse = parse_approval_answer(event, session_id, line)
        if message is not None:
            return message
        if refuse == CLASS_C_NOT_ALWAYS_ALLOWABLE:
            _emit(err, f"{CLASS_C_NOT_ALWAYS_ALLOWABLE}: {CLASS_C_NOT_ALWAYS_ALLOWABLE_MESSAGE}")
            _emit(out, "y / n")
            continue
        _emit(out, offered)
