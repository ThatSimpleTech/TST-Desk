"""Unattended autonomy scheduler (TD-4101 / TD-4103).

After the sign-and-start gate, a daemon-owned session iterates against
the signed charter without waiting on a user message. This module is
the scheduler: prompts, stop reasons, definition-of-done polling, and
the M7 notify. Tool execution stays on the existing dispatcher (the
act seam). Interactive sessions never set ``Session.autonomy``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import ModelConfig
from ..logging import get_logger
from .breakers import maybe_trip
from .charter import Charter
from .supervisor import maybe_check_drift

if TYPE_CHECKING:
    from ..session import Session

log = get_logger("tstd.autonomy.runner")

CLASS_C_STOP = "Class C decision — autonomy stops"
DOD_MET = "definition of done met"
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

    Definition-of-done is polled in ``advance_autonomy`` (TD-4103) and
    wins over the iteration cap on the same turn. Circuit breakers
    (TD-4203) run after the poll on the continue path.
    """
    if class_c:
        return CLASS_C_STOP
    if finished_turns >= charter.caps.max_iterations:
        return f"iteration cap exceeded: {finished_turns} >= {charter.caps.max_iterations}"
    return None


def should_notify(reason: str) -> bool:
    """Class C, cap faults, a met definition of done, and breaker trips.

    A met DoD is a complete stop (TD-4103). The wake-up body is TD-4303.
    Breaker reasons are prefixed ``breaker:`` so TD-4203 can plug in
    without another edit here.
    """
    return (
        reason in (CLASS_C_STOP, DOD_MET)
        or reason.startswith("iteration cap")
        or reason.startswith("spend cap")
        or reason.startswith("wall-clock cap")
        or reason.startswith("breaker:")
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
        if session.dod_poller is not None:
            try:
                poll = await session.dod_poller()
            except Exception:
                log.exception("definition-of-done poll failed")
                poll = None
            session.last_dod_poll = poll
            if poll is not None and poll.all_green:
                session.autonomy_stop_reason = DOD_MET
        if session.autonomy_stop_reason is None:
            reason = maybe_trip(session)
            if reason:
                session.autonomy_stop_reason = reason
        if session.autonomy_stop_reason is None:
            reason = stop_reason(
                charter=charter,
                finished_turns=session.autonomy_turns,
                class_c=session.autonomy_class_c,
            )
            if reason is None:
                try:
                    await maybe_check_drift(session)
                except Exception:
                    log.exception("validator drift check failed")
                await session.add_user_message(continue_prompt(charter, session.autonomy_turns))
                return True
            session.autonomy_stop_reason = reason
    from .wakeup import deliver_wakeup

    await deliver_wakeup(session)
    return False


async def notify_autonomy_stop(config: ModelConfig, message: str) -> None:
    """Deliver *message* on the M7 channels. No-op when they are off.

    *message* is the wake-up body (TD-4303), not a one-line reason.
    """
    from ..notify.ntfy import send as ntfy_send
    from ..notify.slack import send as slack_send

    await slack_send(config, message)
    await ntfy_send(config, message)
