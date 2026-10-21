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
from .hooks import with_hook_context
from .logging import get_logger
from .protocol import AssistantDelta, SteerPoint, UserTurn
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
    """Append the messages sent to steer this turn. Returns how many joined.

    Each is logged as a ``user_turn`` with its steer point, then the
    conversation is saved, and only then does the queue file let it go
    (TD-5155), so a stop in between finds it in the log or the file.
    Each goes in marked as a steer, so context fitting keeps it inside
    this turn rather than treating it as a turn of its own.
    """
    into = session.steering_turn
    if into is None:
        return 0
    steers = session.take_steers(into)
    if not steers:
        return 0
    point = SteerPoint(turn_id=into, step=step, after_tool_call_ids=list(after_tool_call_ids))
    joined = 0
    for queued in steers:
        # TD-5134: a steer is a message the person sent, so user_prompt
        # hooks see it as they see a turn's first message. A block keeps
        # it out of the conversation; the window still shows it, and the
        # hook's run says why.
        prompt_hooks = await session.hooks.run("user_prompt", {"prompt": queued.display})
        if prompt_hooks.block is None:
            joined += 1
            # A /skill typed mid-turn loads before the next step, as it would
            # at the start of a turn; the loop re-assembles the system prompt
            # when one does, so that very step sees it (TD-5160).
            apply_slash_skill(
                session, queued.display, claude_global_fallback=claude_global_fallback
            )
            content = with_hook_context(queued.provider, prompt_hooks.context.strip())
            messages.append(ChatMessage(role="user", content=content, steer=True))
        await session.event_log.add(
            UserTurn(
                session_id=session.id,
                turn_id=queued.id,
                content=queued.display,
                steer=point,
                # Line comments sent mid-turn keep their anchors (TD-5132).
                line_comments=queued.line_comments,
                seq=1,
            )
        )
        if prompt_hooks.block is not None:
            note = f"A hook blocked this message, so it was not sent: {prompt_hooks.block}\n\n"
            await session.event_log.add(AssistantDelta(session_id=session.id, delta=note, seq=1))
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
                "hook_blocked": len(steers) - joined,
                "after_tool_calls": len(point.after_tool_call_ids),
            }
        },
    )
    return joined
