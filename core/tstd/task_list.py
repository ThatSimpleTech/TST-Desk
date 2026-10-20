"""The native loop's task list (TD-5122).

The model keeps it with the ``task_list`` tool (``tools/task_list.py``).
The session holds the list, and every change logs one ``task_list`` event
with the whole list: the Plan pane and the chat's task strip (what the
phone shows) read the newest one. A compaction carries the list into its
summary (``carry_text``), so a model whose earlier turns were summarized
still sees its plan; the tool call that made the list may be gone.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from .protocol import DaemonEvent, TaskList, TaskListItem

if TYPE_CHECKING:
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


def restore_task_list(session: Session, events: Sequence[DaemonEvent]) -> None:
    """On revive, the list is the newest ``task_list`` event's."""
    newest = next((e for e in reversed(events) if isinstance(e, TaskList)), None)
    if newest is not None:
        session.task_list = list(newest.items)


async def keep_task_list_visible(session: Session) -> None:
    """Log the list again when the log window no longer has it.

    The event log keeps the newest events only, so a long session can
    trim its last ``task_list`` away while the list is still current. A
    window attaching then would show no list. Logging it again also puts
    it back on disk, so a restart after that still finds it.
    """
    if not session.task_list:
        return
    if any(isinstance(e, TaskList) for e in session.event_log.all_events):
        return
    await session.event_log.add(
        TaskList(session_id=session.id, items=list(session.task_list), seq=1)
    )
