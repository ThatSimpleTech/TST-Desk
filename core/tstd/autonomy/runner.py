"""Unattended autonomy scheduler (TD-4101).

After the sign-and-start gate, a daemon-owned session iterates against
the signed charter without waiting on a user message. This module is
the scheduler: prompts, stop reasons, and the M7 notify. Tool
execution stays on the existing dispatcher (the act seam).
Interactive sessions never set ``Session.autonomy``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import ModelConfig
from .charter import Charter

if TYPE_CHECKING:
    from ..session import Session

CLASS_C_STOP = "Class C decision — autonomy stops"
CONTINUE_PREFIX = "Continue the autonomous run."


def first_prompt(charter: Charter) -> str:
    """The first turn — the signed objective, not a chat message."""
    done = "\n".join(f"- {item}" for item in charter.definition_of_done)
    stops = "\n".join(f"- {item}" for item in charter.stop_conditions) or "- (none extra)"
    return (
        f"Autonomous run. Do not wait for a user.\n\n"
        f"Objective:\n{charter.objective}\n\n"
        f"Definition of done:\n{done}\n\n"
        f"Stop conditions:\n{stops}\n"
    )


def continue_prompt(charter: Charter, finished_turns: int) -> str:
    """The next iteration after a turn completes without a stop."""
    cap = charter.caps.max_iterations
    return (
        f"{CONTINUE_PREFIX} Objective: {charter.objective}. "
        f"Finished {finished_turns} of {cap} iterations. "
        f"Keep working until a stop condition."
    )


def stop_reason(
    *,
    charter: Charter,
    finished_turns: int,
    class_c: bool,
) -> str | None:
    """Why the scheduler should stop, or ``None`` to enqueue another turn.

    Definition-of-done polling is TD-4103. Drift / circuit breakers are
    E42. This story stops on Class C and the signed iteration cap.
    """
    if class_c:
        return CLASS_C_STOP
    if finished_turns >= charter.caps.max_iterations:
        return f"iteration cap exceeded: {finished_turns} >= {charter.caps.max_iterations}"
    return None


def should_notify(reason: str) -> bool:
    """Class C and cap faults notify (spec §12.2 / §12.7). Clean complete does not."""
    return (
        reason == CLASS_C_STOP
        or reason.startswith("iteration cap")
        or reason.startswith("spend cap")
        or reason.startswith("wall-clock cap")
    )


async def advance_autonomy(session: Session) -> bool:
    """Queue the next iteration, or stop. ``False`` ends the agent loop."""
    charter = session.charter
    if charter is None:
        return False
    if session.autonomy_class_c:
        session.autonomy_stop_reason = CLASS_C_STOP
    elif session.autonomy_stop_reason is None:
        session.autonomy_turns += 1
        reason = stop_reason(
            charter=charter,
            finished_turns=session.autonomy_turns,
            class_c=session.autonomy_class_c,
        )
        if reason is None:
            await session.add_user_message(continue_prompt(charter, session.autonomy_turns))
            return True
        session.autonomy_stop_reason = reason
    reason = session.autonomy_stop_reason
    if session.autonomy_notify is not None and should_notify(reason):
        await session.autonomy_notify(reason)
    return False


async def notify_autonomy_stop(config: ModelConfig, message: str) -> None:
    """Deliver *message* on the M7 channels. No-op when they are off."""
    from ..notify.ntfy import send as ntfy_send
    from ..notify.slack import send as slack_send

    text = f"Autonomy stopped: {message}"
    await slack_send(config, text)
    await ntfy_send(config, text)
