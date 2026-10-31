"""The conversation from before a session's replayed window (TD-5201).

A window rebuilds the chat from the event log, and the log keeps only its
newest events. Every streamed chunk is an event, so one long reply can push
all of a chat's questions out of it, and the window then showed an older
chat as a fragment, or as an empty New chat screen. The saved conversation
still has those messages. This works out which of them come before the
window's first event; ``chat_history_rows`` shapes them for
``conversation_history``.

The two line up by what both of them name: a tool call by its id, when the
id names one message, and a user turn by its number from the start of the
session, where a compaction summary counts the turns it stands for, and by
its words. When they cannot be lined up, the history says so (no messages,
``user_turns`` None) and the window numbers no turn, rather than letting
Edit and Retry fork a turn picked by a guess.
"""

from __future__ import annotations

from collections.abc import Sequence

from .attachments import IMAGE_NOTE_OPENING
from .chat_history_rows import has_images, newest_rows, plain, typed_text
from .compaction import summary_user_turns
from .hooks import HOOK_BLOCKED_NOTE
from .logging import redact_structure
from .protocol import (
    AssistantDelta,
    AssistantReasoning,
    AssistantRetract,
    ConversationHistory,
    ConversationReset,
    DaemonEvent,
    ToolCall,
    ToolResult,
    TurnComplete,
    UserTurn,
)
from .provider import ChatMessage, content_as_text


def restorable_history(
    session_id: str,
    conversation: Sequence[ChatMessage],
    window: Sequence[DaemonEvent],
) -> ConversationHistory:
    """:func:`history_before_window`, scrubbed again as a whole frame.

    Every string was redacted before it was cut; this second pass is the
    same chokepoint the event log uses. Pure, so attach runs it off the
    event loop on copies of the two lists.
    """
    history = history_before_window(session_id, conversation, window)
    return ConversationHistory.model_validate(redact_structure(history.model_dump()))


def history_before_window(
    session_id: str,
    conversation: Sequence[ChatMessage],
    window: Sequence[DaemonEvent],
) -> ConversationHistory:
    """What of *conversation* comes before the event *window*.

    No messages and ``user_turns`` 0 means the window holds the session's
    first turn: nothing before it was lost. ``user_turns`` None means the
    two could not be lined up.
    """
    unknown = ConversationHistory(session_id=session_id, user_turns=None)
    # A fork or a branch switch the window shows both sides of: its user
    # turns count onto neither branch.
    if any(isinstance(event, ConversationReset) for event in window):
        return unknown
    blocked = _blocked(window)
    owners = _turn_owners(conversation)
    shown = sum(
        1 for at, event in enumerate(window) if isinstance(event, UserTurn) and at not in blocked
    )
    first = len(owners) - shown
    if first < 0:
        return unknown
    end, starts_at_first_turn = _boundary(conversation, window, blocked, owners, first)
    if end is None or not _numbering_agrees(conversation, owners, end, first):
        return unknown
    rows, truncated = newest_rows(conversation[:end], owners, first)
    if not rows:
        if not starts_at_first_turn:
            return unknown
        return ConversationHistory(session_id=session_id, user_turns=0)
    return ConversationHistory(
        session_id=session_id, messages=rows, user_turns=first, truncated=truncated
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


def _blocked(window: Sequence[DaemonEvent]) -> set[int]:
    """Positions of the user turns a user_prompt hook blocked, and their notes (TD-5134).

    Each is logged as a user_turn that the daemon answers with its note,
    but it never enters the conversation, so it is no turn of it.
    """
    blocked: set[int] = set()
    for at, event in enumerate(window):
        if not isinstance(event, UserTurn):
            continue
        for later in range(at + 1, len(window)):
            follower = window[later]
            if isinstance(follower, AssistantDelta):
                if follower.delta.startswith(HOOK_BLOCKED_NOTE):
                    blocked.update((at, later))
                break
            if isinstance(follower, UserTurn | ToolCall | ToolResult | AssistantReasoning):
                break
    return blocked


def _boundary(
    conversation: Sequence[ChatMessage],
    window: Sequence[DaemonEvent],
    blocked: set[int],
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
    for at, event in enumerate(window):
        if at in blocked:
            continue
        if isinstance(event, AssistantDelta | AssistantReasoning):
            streamed = True
        elif isinstance(event, ToolCall | ToolResult):
            return _tool_boundary(conversation, event, owners, first), False
        elif isinstance(event, UserTurn):
            return _turn_boundary(conversation, event, owners, first, streamed)
    if not streamed:
        return len(conversation), False
    return _streamed_boundary(conversation, window, blocked), False


def _tool_boundary(
    conversation: Sequence[ChatMessage],
    event: ToolCall | ToolResult,
    owners: Sequence[int],
    first: int,
) -> int | None:
    """At the window's first tool call or result: before its reply, or past its results.

    An id is a key only when it names one message: some servers send none,
    or the same one on every response (``context_clear`` meets them too),
    and the Grok engine keeps no tool calls in the conversation at all. The
    history then stops at the question of the turn the window begins in.
    """
    found = _only_message_with_call(conversation, event.tool_call_id)
    if found is not None:
        end = found if isinstance(event, ToolCall) else _past_results(conversation, found)
        if _numbering_agrees(conversation, owners, end, first):
            return end
    if first == 0 or conversation[owners[first - 1]].role != "user":
        return None
    return owners[first - 1] + 1


def _turn_boundary(
    conversation: Sequence[ChatMessage],
    event: UserTurn,
    owners: Sequence[int],
    first: int,
    streamed: bool,
) -> tuple[int | None, bool]:
    owner = owners[first]
    message = conversation[owner]
    if message.role != "user":
        # A summary, which may stand for turns the window shows as well.
        return owner + 1, False
    # The count says which message this turn is; its words have to agree.
    if not _same_words(message, event.content):
        return None, False
    if streamed:
        return _last_reply_before(conversation, owner), False
    return owner, first == 0


def _same_words(message: ChatMessage, shown: str) -> bool:
    """Whether a user turn the log shows as *shown* is *message*.

    The log keeps what the window displayed: redacted when it went in, and,
    when images went with it, ending in a line that names them where the
    model's copy has the images themselves.
    """
    if has_images(message.content):
        cut = shown.rfind(IMAGE_NOTE_OPENING)
        shown = shown if cut < 0 else shown[:cut]
    return plain(typed_text(message.content)) == plain(shown)


def _numbering_agrees(
    conversation: Sequence[ChatMessage], owners: Sequence[int], end: int, first: int
) -> bool:
    """Whether the turns before *end* are the turns before the window's first.

    A history numbered wrong would point Edit and Retry at the wrong turn.
    A summary kept at the end may stand for turns the window shows as well.
    """
    before = sum(1 for owner in owners if owner < end)
    if before == first:
        return True
    last = conversation[end - 1] if end > 0 else None
    return last is not None and last.role == "system" and before > first


def _only_message_with_call(conversation: Sequence[ChatMessage], call_id: str) -> int | None:
    if not call_id:
        return None
    found = [
        index
        for index, message in enumerate(conversation)
        if any(call.id == call_id for call in message.tool_calls or [])
    ]
    return found[0] if len(found) == 1 else None


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


def _streamed_boundary(
    conversation: Sequence[ChatMessage], window: Sequence[DaemonEvent], blocked: set[int]
) -> int | None:
    """With only streamed chunks in the window: the reply they are the end of.

    Chunks a retry withdrew (TD-5159, TD-5175) are no part of it, and a
    done-check or stop-hook retry ends a turn in more than one message. A
    reply still streaming is in no message yet, and thinking alone cannot
    be matched: the whole conversation is history then. A finished reply
    that matches nothing stops the history at its question.
    """
    withdrawn = {event.response_id for event in window if isinstance(event, AssistantRetract)}
    tail = plain(
        "".join(
            event.delta
            for at, event in enumerate(window)
            if at not in blocked
            and isinstance(event, AssistantDelta)
            and (event.response_id is None or event.response_id not in withdrawn)
        )
    )
    if tail:
        joined = ""
        for index in range(len(conversation) - 1, -1, -1):
            message = conversation[index]
            if message.role == "user" and not message.check:
                break
            if message.role != "assistant":
                continue
            text = plain(content_as_text(message.content))
            joined = text + joined
            if joined.endswith(tail) or text.endswith(tail):
                return index
    finished = any(
        isinstance(event, TurnComplete) for at, event in enumerate(window) if at not in blocked
    )
    if not finished:
        return len(conversation)
    for index in range(len(conversation) - 1, -1, -1):
        if conversation[index].role == "user" and not conversation[index].check:
            return index + 1
    return None
