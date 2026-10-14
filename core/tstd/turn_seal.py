"""Seal a turn a restart cut in half (TD-5018).

A daemon that dies mid-turn leaves a log whose last ``user_turn`` has no
``turn_complete`` after it, and often a conversation whose last
assistant message asked for tools that never answered. Revived as is,
the window shows a turn that never ends, and the next request carries
tool calls with no tool reply, which providers refuse. Both functions
here are pure: the daemon decides when to call them and what to do with
the answer.
"""

from __future__ import annotations

from collections.abc import Sequence

from .protocol import DaemonEvent, TurnComplete, UserTurn
from .provider import ChatMessage

# The tool reply a cut call gets. It says what is known (the call may
# have partly run) and nothing more.
CUT_BY_RESTART = (
    "Not finished: TST Desk restarted during this call. It may have partly run; "
    "check before running it again."
)
INTERRUPTED_BY_RESTART = "interrupted_by_restart"


def open_turn(events: Sequence[DaemonEvent]) -> bool:
    """True when the last ``user_turn`` has no ``turn_complete`` after it."""
    last_user = 0
    last_complete = 0
    for event in events:
        if isinstance(event, UserTurn):
            last_user = max(last_user, event.seq)
        elif isinstance(event, TurnComplete):
            last_complete = max(last_complete, event.seq)
    return last_user > last_complete


def repair_cut_conversation(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    """*messages* with a reply for every tool call the trailing assistant left open.

    Only the last assistant message with tool calls can be cut: every
    earlier one was answered before the loop sent the next request. Tool
    replies that did land stay where they are, and the missing ones are
    appended in call order. A conversation with nothing open comes back
    unchanged.
    """
    repaired = list(messages)
    index = len(repaired) - 1
    while index >= 0 and repaired[index].role == "tool":
        index -= 1
    if index < 0:
        return repaired
    assistant = repaired[index]
    if assistant.role != "assistant" or not assistant.tool_calls:
        return repaired
    answered = {message.tool_call_id for message in repaired[index + 1 :]}
    for call in assistant.tool_calls:
        if call.id not in answered:
            repaired.append(ChatMessage(role="tool", content=CUT_BY_RESTART, tool_call_id=call.id))
    return repaired
