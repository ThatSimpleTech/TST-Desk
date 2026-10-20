"""Steer a running turn (TD-5112).

A message sent while a turn runs used to wait for the whole turn. The
loop now hands it to that turn at the next tool-batch boundary: after the
batch's results are on the conversation, just before the next model call
(after any cap or progress-guard pause, so one sent while paused counts).
It goes in as an ordinary user message, so the model reads it before its
next step and the turn carries on. The daemon does this, not the window,
so a phone or a second window steers the same way (AGENTS §2.5).

The ``user_turn`` it logs names the point (:class:`SteerPoint`), which the
audit writer records. Stop is untouched: a stop seals the turn before this
runs, and a message still waiting then starts the next turn as before.
"""

from __future__ import annotations

from collections.abc import Sequence

from .context.skills import apply_slash_skill
from .logging import get_logger
from .protocol import SteerPoint, UserTurn
from .provider import ChatMessage
from .session import Session

log = get_logger("tstd.steer")


async def inject_steers(
    session: Session,
    messages: list[ChatMessage],
    *,
    step: int,
    after_tool_call_ids: Sequence[str],
    claude_global_fallback: bool | None = None,
) -> int:
    """Append the messages sent while this turn ran. Returns how many.

    Each is logged as a ``user_turn`` with its steer point, then the
    conversation is saved, and only then does the queue file let it go
    (TD-5155), so a stop in between finds it in the log or the file.
    """
    into = session.steering_turn
    if into is None:
        return 0
    steers = session.take_steers()
    if not steers:
        return 0
    point = SteerPoint(turn_id=into, step=step, after_tool_call_ids=list(after_tool_call_ids))
    for queued in steers:
        # A /skill typed mid-turn loads before the next step, as it would
        # at the start of a turn.
        apply_slash_skill(session, queued.display, claude_global_fallback=claude_global_fallback)
        messages.append(ChatMessage(role="user", content=queued.provider))
        await session.event_log.add(
            UserTurn(
                session_id=session.id,
                turn_id=queued.id,
                content=queued.display,
                steer=point,
                seq=1,
            )
        )
    await session.conversation_changed()
    for queued in steers:
        await session.user_turn_logged(queued)
    log.info(
        "turn steered",
        extra={
            "extra_fields": {
                "session_id": session.id,
                "turn_id": into,
                "step": step,
                "messages": len(steers),
                "after_tool_calls": len(point.after_tool_call_ids),
            }
        },
    )
    return len(steers)
