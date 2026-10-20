"""Withdraw a stalled partial reply and ask again (TD-5159).

TD-5106's watchdog ends a stream that goes quiet. Before any output the
provider retries it under ``provider_retry``. After output began the
provider cannot: the window already shows the partial text, and a second
attempt would add a second reply below it. So the loop withdraws that
response first, with an ``assistant_retract`` naming the ``response_id``
its deltas carried, and then sends the same request once more.

Only the model call that stalled is redone. Tool calls that already ran
in the turn are in the conversation as results and are not run again; a
tool call the stalled response had started to stream was never run. The
partial text never entered the conversation either (TD-5152), so the
retry sees exactly what the first attempt saw.

The off-turn calls (the work check, memory distill) never come here and
keep TD-5146's rule: a timeout, a stall included, is returned, not resent.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, NamedTuple

from .config import ProviderRetryConfig
from .logging import get_logger
from .protocol import AssistantDelta, AssistantRetract, DaemonEvent
from .provider import RetryConfig, retry_delay

if TYPE_CHECKING:
    from .session import Session

log = get_logger("tstd.reply_retry")

# The codes a turn withdraws and retries once output has begun. A cut-off
# stream (``stream_interrupted``) is not here: the gateway reported an
# error or gave up, and resending into it is the user's call (TD-5152).
WITHDRAWN_CODES = frozenset({"stream_stalled"})

# At most this many withdraw-and-retry rounds per model call, and never
# more than ``provider_retry.max_retries`` (0 turns it off). Each round can
# wait a whole ``stream_idle_timeout`` before it gives up.
STALL_RETRIES = 1


class StreamResult(NamedTuple):
    """One streamed model call, as the loop consumes it."""

    content: str
    tool_calls: dict[int, dict[str, str | int]]
    failed: bool
    error_msg: str
    error_code: str | None
    raw_detail: str
    finish_reason: str | None
    reasoning_details: list[dict[str, Any]] | None
    # A chunk arrived before the call ended. Past that point the provider
    # no longer retries, and the window may be showing text or thinking.
    began: bool
    # The provider's own retry flag on the error that ended the call: a
    # failure of the endpoint, not of the request (TD-5120). False on
    # success, Stop and cancel.
    retryable: bool = False
    # The id this call's deltas carried, so a backup can withdraw them.
    response_id: str | None = None


def new_response_id() -> str:
    """A fresh id for one model call's deltas."""
    return f"resp-{uuid.uuid4().hex[:16]}"


def stall_retries(retry: ProviderRetryConfig) -> int:
    """How many times a turn redoes a call that stalled mid-reply."""
    return min(STALL_RETRIES, retry.max_retries)


def _withdrawable(result: StreamResult, session: Session, retried: int, allowed: int) -> bool:
    if session.cancel_requested or session.stop_turn_requested:
        return False
    return (
        result.failed
        and result.began
        and result.error_code in WITHDRAWN_CODES
        and retried < allowed
    )


async def stream_reply(
    attempt: Callable[[str], Awaitable[StreamResult]],
    session: Session,
    retry: ProviderRetryConfig,
) -> StreamResult:
    """Run *attempt* under a fresh response id; withdraw and redo a stall.

    *attempt* streams one model call, stamping its deltas with the id it is
    given. A call that stalls after output began is withdrawn
    (``assistant_retract``) and, after ``provider_retry``'s first backoff,
    run again under a new id. Stop or cancel during the wait ends it with
    nothing kept: the withdrawn text is not the reply.
    """
    allowed = stall_retries(retry)
    retried = 0
    while True:
        response_id = new_response_id()
        result = await attempt(response_id)
        if not _withdrawable(result, session, retried, allowed):
            return result
        await session.event_log.add(
            AssistantRetract(
                session_id=session.id,
                response_id=response_id,
                reason=result.error_code or "stream_stalled",
                seq=1,
            )
        )
        retried += 1
        delay = retry_delay(
            retried,
            RetryConfig(
                max_retries=retry.max_retries,
                initial_delay=retry.initial_delay,
                max_delay=retry.max_delay,
            ),
        )
        log.warning(
            "reply withdrawn after a stall, asking again",
            extra={
                "extra_fields": {
                    "session_id": session.id,
                    "code": result.error_code,
                    "attempt": retried,
                    "max_retries": allowed,
                    "delay": f"{delay:.2f}s",
                }
            },
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(session.wait_for_turn_abort(), delay)
        if session.cancel_requested or session.stop_turn_requested:
            return result._replace(
                content="",
                tool_calls={},
                error_msg="cancelled" if session.cancel_requested else "stopped",
                error_code=None,
                raw_detail="",
                reasoning_details=None,
            )


def withdrawn_responses(events: Sequence[DaemonEvent]) -> set[str]:
    """The response ids an ``assistant_retract`` in *events* withdrew."""
    return {e.response_id for e in events if isinstance(e, AssistantRetract)}


def shown_deltas(events: Sequence[DaemonEvent], start: int = 0) -> list[str]:
    """Assistant text from ``events[start:]`` that is still the reply.

    A delta whose response was withdrawn later in *events* is left out;
    text with no response id (a failure line, a progress note, an older
    daemon) always stays.
    """
    withdrawn = withdrawn_responses(events)
    return [
        e.delta
        for e in events[start:]
        if isinstance(e, AssistantDelta)
        and (e.response_id is None or e.response_id not in withdrawn)
    ]
