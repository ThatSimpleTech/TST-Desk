"""The session goal ``/goal`` sets (TD-5124).

``/goal <statement>`` gives one session a goal: the statement becomes the
turn's task, and the done-check (``stop_check.py``) judges it as a worker
statement every time the model ends a turn, until a check passes with it.
``/goal`` alone shows it and ``/goal clear`` clears it, with no model call.
The loop reads the command when the turn starts, so the window, the phone
and ``tst run`` all reach it the same way. Each change logs a
``session_goal`` event; the newest one is the goal, and a restart restores
it from the log.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .protocol import DaemonEvent, SessionGoal

if TYPE_CHECKING:
    from .session import Session

#: The longest goal ``/goal`` takes, in characters.
MAX_GOAL_CHARS = 4000
#: ``/goal <word>`` that clears the goal instead of setting it.
GOAL_CLEAR_WORDS = frozenset({"clear", "off", "stop", "none", "cancel", "reset"})

_GOAL_COMMAND = re.compile(r"^\s*/goal(?=\s|$)(.*)$", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class GoalCommand:
    """A ``/goal`` message: set, show (``status``) or clear the goal."""

    kind: Literal["set", "status", "clear", "too_long"]
    text: str = ""


def parse_goal_command(text: str) -> GoalCommand | None:
    """Read ``/goal``, ``/goal clear`` or ``/goal <statement>``; None otherwise."""
    match = _GOAL_COMMAND.match(text)
    if match is None:
        return None
    arg = match.group(1).strip()
    if not arg:
        return GoalCommand("status")
    if arg.lower() in GOAL_CLEAR_WORDS:
        return GoalCommand("clear")
    if len(arg) > MAX_GOAL_CHARS:
        return GoalCommand("too_long", arg)
    return GoalCommand("set", arg)


def goal_directive(goal: str) -> str:
    """What the model is sent for ``/goal <statement>``: the goal, as the task."""
    return (
        f"Goal for this session: {goal}\n\n"
        "Work toward it now. When you end your turn, a check decides whether the "
        "goal holds; until it does, you will be told it is not met yet."
    )


def goal_provider_content(goal: str, provider: str | list[dict[str, Any]]) -> Any:
    """The turn's provider content with the ``/goal`` text replaced by the directive.

    Attached images stay; the typed text is the command, not the task.
    """
    directive = goal_directive(goal)
    if isinstance(provider, str):
        return directive
    others = [part for part in provider if part.get("type") != "text"]
    return [{"type": "text", "text": directive}, *others]


async def set_goal(
    session: Session, goal: str | None, change: Literal["set", "cleared", "met"]
) -> None:
    """Change the session's goal and log it."""
    session.goal = goal
    session.goal_checks = 0
    await session.event_log.add(SessionGoal(session_id=session.id, goal=goal, change=change, seq=1))


async def answer_goal_command(session: Session, command: GoalCommand) -> str:
    """Do a ``/goal`` that needs no model call; return the note the chat shows."""
    current = session.goal
    if command.kind == "clear":
        if current is None:
            return "No goal set."
        await set_goal(session, None, "cleared")
        return f"Goal cleared: {current}"
    if command.kind == "too_long":
        return (
            f"Goal not set: it is {len(command.text)} characters, "
            f"and a goal can be at most {MAX_GOAL_CHARS}."
        )
    if current is None:
        return (
            "No goal set. Set one with /goal followed by what should be true when the work is done."
        )
    checks = session.goal_checks
    if checks == 0:
        checked = "Not checked yet."
    else:
        checked = f"Checked {checks} time{'s' if checks != 1 else ''} so far."
    return f"Goal: {current}\n\n{checked} Clear it with /goal clear."


def restore_goal(session: Session, events: Sequence[DaemonEvent]) -> None:
    """On revive, the goal is the newest ``session_goal`` event's."""
    newest = next((e for e in reversed(events) if isinstance(e, SessionGoal)), None)
    if newest is not None:
        session.goal = newest.goal


async def keep_goal_visible(session: Session) -> None:
    """Log the goal again when the log window no longer has it.

    The newest ``session_goal`` event is the goal's only copy on disk, so
    a rotation that drops it, or an attach that finds it missing, logs it
    again (the TD-5122 task-list pattern).
    """
    if session.goal is None:
        return
    if any(isinstance(e, SessionGoal) for e in session.event_log.all_events):
        return
    await session.event_log.add(
        SessionGoal(session_id=session.id, goal=session.goal, change="set", seq=1)
    )
