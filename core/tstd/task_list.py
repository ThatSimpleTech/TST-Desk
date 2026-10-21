"""The native loop's task list (TD-5122).

The model keeps it with the ``task_list`` tool (``tools/task_list.py``).
The session holds the list, and every change logs one ``task_list`` event
with the whole list: the Plan pane and the chat's task strip (what the
phone shows) read the newest one. A compaction carries the list into its
summary (``carry_text``), so a model whose earlier turns were summarized
still sees its plan; the tool call that made the list may be gone.
The newest ``task_list`` event is also the copy a restart reads, so the
daemon logs the list again whenever the log window rotates it out.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from pydantic import ValidationError

from .compaction_text import is_summary
from .protocol import DaemonEvent, TaskList, TaskListItem

if TYPE_CHECKING:
    from .provider import ChatMessage
    from .session import Session

_LABELS = {"pending": "pending", "in_progress": "in progress", "done": "done"}


def render_tasks(items: Sequence[TaskListItem]) -> str:
    """One numbered line per item, its status in brackets."""
    return "\n".join(
        f"{n}. [{_LABELS[item.status]}] {item.content}" for n, item in enumerate(items, start=1)
    )


def progress(items: Sequence[TaskListItem]) -> str:
    """``2 of 5 done``, or ``all 3 done``."""
    done = sum(1 for item in items if item.status == "done")
    return f"all {done} done" if done == len(items) else f"{done} of {len(items)} done"


def saved_text(items: Sequence[TaskListItem]) -> str:
    """The tool result: what the model's list now is."""
    if not items:
        return "Task list cleared."
    return f"Task list saved: {progress(items)}.\n{render_tasks(items)}"


def carry_text(items: Sequence[TaskListItem]) -> str:
    """What a compaction carries for *items*; empty when there is no list."""
    if not items:
        return ""
    return (
        f"Your task list as of this summary ({progress(items)}). "
        "Keep working from it; a later task_list call replaces it.\n" + render_tasks(items)
    )


async def set_task_list(session: Session, items: Sequence[TaskListItem]) -> None:
    """Replace the session's list and log it."""
    session.task_list = list(items)
    await session.event_log.add(TaskList(session_id=session.id, items=list(items), seq=1))


def _saved(result: ChatMessage | None) -> bool:
    text = result.content if result is not None else None
    return isinstance(text, str) and ("Task list saved:" in text or "Task list cleared." in text)


def branch_task_list(
    conversation: Sequence[ChatMessage], current: Sequence[TaskListItem]
) -> list[TaskListItem]:
    """The list as the branch *conversation* left it (TD-5161).

    The newest ``task_list`` call the branch made and saved wins. A branch
    whose newest record is a compaction summary keeps *current*: the
    summary carried a list this module does not parse back. A branch with
    neither never made a list, so it has none.
    """
    results = {m.tool_call_id: m for m in conversation if m.role == "tool"}
    for message in reversed(conversation):
        if is_summary(message):
            return list(current)
        for call in reversed(message.tool_calls or []):
            fn = call.function
            if fn is None or fn.name != "task_list" or not _saved(results.get(call.id)):
                continue
            try:
                tasks = json.loads(fn.arguments)["tasks"]
                return [
                    TaskListItem.model_validate(
                        {"content": " ".join(t["content"].split()), "status": t["status"]}
                    )
                    for t in tasks
                ]
            except (ValueError, KeyError, TypeError, ValidationError):
                continue
    return []


async def follow_branch(session: Session) -> None:
    """After an edit or a sibling switch, show the branch's own list.

    Otherwise the next compaction would carry the abandoned branch's plan
    into a branch that never made it, worded as the plan to keep working
    from, and the Plan pane would keep showing it.
    """
    items = branch_task_list(session.conversation, session.task_list)
    if items != session.task_list:
        await set_task_list(session, items)


def restore_task_list(session: Session, events: Sequence[DaemonEvent]) -> None:
    """On revive, the list is the newest ``task_list`` event's."""
    newest = next((e for e in reversed(events) if isinstance(e, TaskList)), None)
    if newest is not None:
        session.task_list = list(newest.items)


async def keep_task_list_visible(session: Session) -> None:
    """Log the list again when the log window no longer has it.

    The event log keeps the newest events only, so a long session can
    trim its last ``task_list`` away while the list is still current.
    That event is the list's only copy on disk, so the daemon calls this
    as soon as a rotation drops it: a restart then still finds the list,
    and the next compaction still carries it. An attach calls it too, so
    a window attaching never shows an empty Plan pane.
    """
    if not session.task_list:
        return
    if any(isinstance(e, TaskList) for e in session.event_log.all_events):
        return
    await session.event_log.add(
        TaskList(session_id=session.id, items=list(session.task_list), seq=1)
    )
