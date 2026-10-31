"""The saved conversation's messages as ``conversation_history`` rows (TD-5201).

``chat_history`` decides where the history stops; this shapes what comes
before it. One attach sends it in one frame, and a long session's
conversation can hold megabytes, so every part is capped, and the walk
starts at the newest message and stops once the rows fill the frame.

Every string is redacted before it is cut. The patterns match whole
secrets only, so a secret cut in half by a cap would get past them, while
the replay's copy of the same text was scrubbed whole when it was logged.
Tool results are not restored at all: they can hold any text a tool read,
and an exact-text secret that scrubbed one when it was logged (an MCP
server's token) is unknown to a later process that never read it.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Sequence
from typing import Any

from .hooks import HOOK_NOTE_HEADER
from .logging import redact_secrets
from .plan_mode import PLAN_NOTE
from .protocol import HistoryMessage, HistoryTool
from .provider import ChatMessage, MessageContent, content_as_text
from .tools.delegate_named import WORKER_NOTE_PREFIX

MAX_ROWS = 400
TEXT_CAP = 20_000
ARGUMENT_CAP = 2_000
TOTAL_CAP = 1_500_000


def newest_rows(
    messages: Sequence[ChatMessage], owners: Sequence[int], first: int
) -> tuple[list[HistoryMessage], bool]:
    """The chat's messages, in order, and whether older ones were left out.

    No system prompt, no done-check feedback, no tool results. *owners*
    holds the message of each user turn (a summary once per turn it
    stands for); *first* is the window's first turn.
    """
    turn_of = {owner: turn for turn, owner in enumerate(owners)}
    rows: list[HistoryMessage] = []
    total = 0
    for index in range(len(messages) - 1, -1, -1):
        row = _row(messages[index], index, turn_of, owners, first)
        if row is None:
            continue
        size = _size(row)
        if len(rows) == MAX_ROWS or total + size > TOTAL_CAP:
            rows.reverse()
            return rows, True
        rows.append(row)
        total += size
    rows.reverse()
    return rows, False


def _row(
    message: ChatMessage,
    index: int,
    turn_of: dict[int, int],
    owners: Sequence[int],
    first: int,
) -> HistoryMessage | None:
    if index == 0 and message.role == "system":
        return None
    if message.role == "user":
        if message.check:
            return None
        text = _cap(_user_text(message.content), TEXT_CAP)
        return HistoryMessage(role="user", text=text, user_index=turn_of.get(index))
    if message.role == "assistant":
        return _assistant_row(message)
    if message.role == "tool":
        return None
    # A summary, for the turns it stands for that the window does not show.
    folded = sum(1 for turn, owner in enumerate(owners) if owner == index and turn < first)
    if not folded:
        return None
    text = _cap(content_as_text(message.content), TEXT_CAP)
    return HistoryMessage(role="summary", text=text, folded_turns=folded)


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


def typed_text(content: MessageContent | None) -> str:
    """A user message's own words: its text without the notes the daemon added."""
    if content is None:
        return ""
    prefixes = _note_prefixes()
    if isinstance(content, str):
        return _without_notes(content, prefixes)
    texts = [
        _without_notes(part["text"], prefixes)
        for part in content
        if part.get("type") == "text" and isinstance(part.get("text"), str)
    ]
    return "\n\n".join(text for text in texts if text)


def has_images(content: MessageContent | None) -> bool:
    return _image_count(content) > 0


def _image_count(content: MessageContent | None) -> int:
    if content is None or isinstance(content, str):
        return 0
    return sum(1 for part in content if part.get("type") == "image_url")


def _user_text(content: MessageContent | None) -> str:
    """A user message as typed. Images are counted; their bytes never go out."""
    text = typed_text(content)
    images = _image_count(content)
    if not images:
        return text
    count = f"[{images} image{'' if images == 1 else 's'} attached]"
    return f"{text}\n\n{count}" if text else count


def plain(text: str) -> str:
    """*text* redacted with no whitespace, for telling whether two copies agree.

    The log keeps text as it streamed, in chunks and redacted chunk by
    chunk; the conversation keeps it whole and raw.
    """
    return "".join(redact_secrets(text).split())


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
        return {_cap(str(k), ARGUMENT_CAP): _cap_strings(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_cap_strings(v, depth + 1) for v in value]
    return value


def _cap(text: str, limit: int) -> str:
    """*text* redacted, then cut to *limit*: never the other way round."""
    text = redact_secrets(text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …"


def _size(row: HistoryMessage) -> int:
    return len(row.text) + sum(len(t.name) + len(json.dumps(t.arguments)) for t in row.tools)
