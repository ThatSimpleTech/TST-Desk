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
from typing import Any

from .context.skills import apply_slash_skill
from .hooks import HOOK_BLOCKED_NOTE, with_hook_context
from .logging import get_logger
from .protocol import AssistantDelta, SteerPoint, UserTurn
from .provider import ChatMessage
from .session import Session
from .session_goal import answer_goal_command, goal_provider_content, parse_goal_command, set_goal

log = get_logger("tstd.steer")


async def _apply_goal(
    session: Session, display: str, provider: str | list[dict[str, Any]]
) -> str | list[dict[str, Any]] | None:
    """A ``/goal`` sent mid-turn changes the goal now (TD-5161).

    It is read as at the start of a turn, so ``/goal clear`` ends a goal
    chase without Stop. Returns what the model reads in its place (the
    directive, or the note the command answers with), or None when the
    message is not ``/goal``.
    """
    command = None if session.autonomy else parse_goal_command(display)
    if command is None:
        return None
    if command.kind == "set":
        await set_goal(session, command.text, "set")
        content: str | list[dict[str, Any]] = goal_provider_content(command.text, provider)
        return content
    return await answer_goal_command(session, command)


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
            provider = await _apply_goal(session, queued.display, queued.provider)
            if provider is None:
                # A /skill typed mid-turn loads before the next step, as it
                # would at the start of a turn; the loop re-assembles the
                # system prompt when one does, so that step sees it (TD-5160).
                apply_slash_skill(
                    session, queued.display, claude_global_fallback=claude_global_fallback
                )
                provider = queued.provider
            content = with_hook_context(provider, prompt_hooks.context.strip())
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
            note = f"{HOOK_BLOCKED_NOTE}{prompt_hooks.block}\n\n"
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
