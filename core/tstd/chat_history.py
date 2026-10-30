"""The conversation from before a session's replayed window (TD-5201).

A window rebuilds the chat from the event log, and the log keeps only its
newest events. Every streamed chunk is an event, so one long reply can push
all of a chat's questions out of it, and the window then showed an older
chat as a fragment, or as an empty New chat screen. The saved conversation
still has those messages. This works out which of them come before the
window's first event and shapes them for ``conversation_history``.

The two line up by what both of them name: a tool call by its id, and a
user turn by its number from the start of the session, where a compaction
summary counts the turns it stands for. When they cannot be lined up, no
history is sent and the window says earlier messages are not stored.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Sequence
from typing import Any

from .compaction import summary_user_turns
from .hooks import HOOK_NOTE_HEADER
from .plan_mode import PLAN_NOTE
from .protocol import (
    AssistantDelta,
    AssistantReasoning,
    ConversationHistory,
    DaemonEvent,
    HistoryMessage,
    HistoryTool,
    ToolCall,
    ToolResult,
    UserTurn,
)
from .provider import ChatMessage, MessageContent, content_as_text
from .tools.delegate_named import WORKER_NOTE_PREFIX

# One attach sends this in one frame, and a long session's conversation can
# hold megabytes of tool output, so every part of it is capped.
MAX_ROWS = 400
TEXT_CAP = 20_000
OUTPUT_CAP = 4_000
ARGUMENT_CAP = 2_000
TOTAL_CAP = 1_500_000


def history_before_window(
    session_id: str,
    conversation: Sequence[ChatMessage],
    window: Sequence[DaemonEvent],
) -> ConversationHistory | None:
    """What of *conversation* comes before the event *window*, or None when unknown.

    An empty history (no messages) means the window holds the session's
    first turn: nothing before it was lost.
    """
    owners = _turn_owners(conversation)
    shown = sum(1 for event in window if isinstance(event, UserTurn))
    first = len(owners) - shown
    if first < 0:
        return None
    end, starts_at_first_turn = _boundary(conversation, window, owners, first)
    if end is None or not _numbering_agrees(conversation, owners, end, first):
        return None
    rows = _rows(conversation[:end], owners, first)
    if not rows:
        if not starts_at_first_turn:
            return None
        return ConversationHistory(session_id=session_id, user_turns=0)
    kept = _within_caps(rows)
    return ConversationHistory(
        session_id=session_id,
        messages=kept,
        user_turns=first,
        truncated=len(kept) < len(rows),
    )


def _turn_owners(conversation: Sequence[ChatMessage]) -> list[int]:
    """For each user turn of the session, the index of the message holding it.

    The same count as ``compaction.window_user_turns``, except that a turn
    inside a summary names the summary instead of None.
    """
    owners: list[int] = []
    for index, message in enumerate(conversation):
        if index == 0 and message.role == "system":
            continue
        if message.role == "user":
            if not message.check:
                owners.append(index)
            continue
        folded = summary_user_turns(message)
        if folded:
            owners.extend([index] * folded)
    return owners


def _boundary(
    conversation: Sequence[ChatMessage],
    window: Sequence[DaemonEvent],
    owners: Sequence[int],
    first: int,
) -> tuple[int | None, bool]:
    """Where the history stops, and whether the window starts at the first turn.

    The first event in the window that names a conversation message decides.
    Chunks streamed before it belong to the message it names (a tool call's
    own reply) or to the reply that ended the turn before it (a user turn),
    so those messages stay out of the history: the window shows them.
    """
    streamed = False
    for event in window:
        if isinstance(event, AssistantDelta | AssistantReasoning):
            streamed = True
        elif isinstance(event, ToolCall):
            return _message_with_call(conversation, event.tool_call_id), False
        elif isinstance(event, ToolResult):
            found = _message_with_call(conversation, event.tool_call_id)
            return (None if found is None else _past_results(conversation, found)), False
        elif isinstance(event, UserTurn):
            owner = owners[first]
            if conversation[owner].role != "user":
                return owner + 1, False
            if streamed:
                return _last_reply_before(conversation, owner), False
            return owner, first == 0
    if not streamed:
        return len(conversation), False
    return _streamed_tail_start(conversation, window), False


def _numbering_agrees(
    conversation: Sequence[ChatMessage], owners: Sequence[int], end: int, first: int
) -> bool:
    """Whether the turns before *end* are the turns before the window's first.

    They are not after a fork the window still shows both sides of, and a
    history numbered wrong would point Edit and Retry at the wrong turn. A
    summary kept at the end may stand for turns the window shows as well.
    """
    before = sum(1 for owner in owners if owner < end)
    if before == first:
        return True
    last = conversation[end - 1] if end > 0 else None
    return last is not None and last.role == "system" and before > first


def _message_with_call(conversation: Sequence[ChatMessage], call_id: str) -> int | None:
    for index in range(len(conversation) - 1, -1, -1):
        calls = conversation[index].tool_calls or []
        if any(call.id == call_id for call in calls):
            return index
    return None


def _past_results(conversation: Sequence[ChatMessage], index: int) -> int:
    """The first message after the assistant message at *index* and its results."""
    end = index + 1
    while end < len(conversation) and conversation[end].role == "tool":
        end += 1
    return end


def _last_reply_before(conversation: Sequence[ChatMessage], owner: int) -> int:
    """The reply that ended the turn before the user message at *owner*."""
    for index in range(owner - 1, -1, -1):
        role = conversation[index].role
        if role == "assistant":
            return index
        if role == "user" and not conversation[index].check:
            break
    return owner


def _streamed_tail_start(conversation: Sequence[ChatMessage], window: Sequence[DaemonEvent]) -> int:
    """With only streamed chunks in the window: the reply they are the end of.

    A reply still streaming is in no message yet, and thinking alone cannot
    be matched, so the whole conversation is history then.
    """
    tail = "".join(e.delta for e in window if isinstance(e, AssistantDelta)).strip()
    if tail:
        for index in range(len(conversation) - 1, -1, -1):
            message = conversation[index]
            if message.role != "assistant":
                continue
            if content_as_text(message.content).strip().endswith(tail):
                return index
            break
    return len(conversation)


def _rows(
    messages: Sequence[ChatMessage], owners: Sequence[int], first: int
) -> list[HistoryMessage]:
    """The chat's messages, in order: no system prompt, no done-check feedback."""
    turn_of = {owner: turn for turn, owner in enumerate(owners) if owner < len(messages)}
    rows: list[HistoryMessage] = []
    for index, message in enumerate(messages):
        if index == 0 and message.role == "system":
            continue
        if message.role == "user":
            if not message.check:
                rows.append(
                    HistoryMessage(
                        role="user",
                        text=_cap(_user_text(message.content), TEXT_CAP),
                        user_index=turn_of.get(index),
                    )
                )
        elif message.role == "assistant":
            row = _assistant_row(message)
            if row is not None:
                rows.append(row)
        elif message.role == "tool":
            _attach_output(rows, message)
        else:
            folded = _folded_before(owners, index, first)
            if folded:
                text = _cap(content_as_text(message.content), TEXT_CAP)
                rows.append(HistoryMessage(role="summary", text=text, folded_turns=folded))
    return rows


def _folded_before(owners: Sequence[int], index: int, first: int) -> int:
    """Turns the summary at *index* stands for that the window does not show."""
    return sum(1 for turn, owner in enumerate(owners) if owner == index and turn < first)


def _assistant_row(message: ChatMessage) -> HistoryMessage | None:
    tools = [
        HistoryTool(
            tool_call_id=call.id,
            name=call.function.name,
            arguments=_arguments(call.function.arguments),
        )
        for call in message.tool_calls or []
        if call.function is not None
    ]
    text = _cap(content_as_text(message.content).strip(), TEXT_CAP)
    if not text and not tools:
        return None
    return HistoryMessage(role="assistant", text=text, tools=tools)


def _attach_output(rows: list[HistoryMessage], message: ChatMessage) -> None:
    output = _cap(content_as_text(message.content), OUTPUT_CAP)
    for row in reversed(rows):
        for tool in row.tools:
            if tool.tool_call_id == message.tool_call_id:
                tool.output = output
                return


@functools.cache
def _note_prefixes() -> tuple[str, ...]:
    """How each note the daemon adds after a person's words begins.

    Only the opening sentence, so a note worded differently in an older
    version still matches. Imported here: the workflow tool's module pulls
    in the session and the delegate machinery.
    """
    from .tools.workflow import ULTRACODE_NOTE

    opening = (PLAN_NOTE, ULTRACODE_NOTE)
    return (
        *(note[: note.index(".") + 1] for note in opening),
        HOOK_NOTE_HEADER,
        WORKER_NOTE_PREFIX,
    )


def _without_notes(text: str, prefixes: Sequence[str]) -> str:
    """The person's own words: *text* cut where the first added note begins."""
    if any(text.startswith(prefix) for prefix in prefixes):
        return ""
    cut = len(text)
    for prefix in prefixes:
        at = text.find("\n\n" + prefix)
        if at >= 0:
            cut = min(cut, at)
    return text[:cut]


def _user_text(content: MessageContent | None) -> str:
    """A user message as typed. Images are counted; their bytes never go out."""
    prefixes = _note_prefixes()
    if content is None:
        return ""
    if isinstance(content, str):
        return _without_notes(content, prefixes)
    texts: list[str] = []
    images = 0
    for part in content:
        if part.get("type") == "image_url":
            images += 1
        elif part.get("type") == "text" and isinstance(part.get("text"), str):
            text = _without_notes(part["text"], prefixes)
            if text:
                texts.append(text)
    if images:
        texts.append(f"[{images} image{'' if images == 1 else 's'} attached]")
    return "\n\n".join(texts)


def _arguments(raw: str) -> dict[str, Any]:
    """A call's arguments as the window shows them: parsed, long strings cut."""
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        return {"arguments": _cap(raw, ARGUMENT_CAP)}
    if not isinstance(parsed, dict):
        return {"arguments": _cap(raw, ARGUMENT_CAP)}
    capped = _cap_strings(parsed, depth=0)
    return capped if isinstance(capped, dict) else {}


def _cap_strings(value: object, depth: int) -> object:
    if isinstance(value, str):
        return _cap(value, ARGUMENT_CAP)
    if depth >= 4:
        return value if isinstance(value, int | float | bool) or value is None else "…"
    if isinstance(value, dict):
        return {str(k): _cap_strings(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_cap_strings(v, depth + 1) for v in value]
    return value


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …"


def _size(row: HistoryMessage) -> int:
    return len(row.text) + sum(
        len(t.output or "") + len(json.dumps(t.arguments)) for t in row.tools
    )


def _within_caps(rows: list[HistoryMessage]) -> list[HistoryMessage]:
    """The newest rows that fit: older ones go first, as the log's own window does."""
    kept = rows[-MAX_ROWS:]
    total = sum(_size(row) for row in kept)
    while kept and total > TOTAL_CAP:
        total -= _size(kept[0])
        kept = kept[1:]
    return kept
