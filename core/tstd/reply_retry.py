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

A reply that spent the whole output cap thinking (TD-5233) is not a
fault of the endpoint, so it has its own allowance: it is withdrawn the
same way and asked once more, straight away, with a larger ``max_tokens``
(output_cap.py sizes it). When the endpoint refuses the larger request
before any output (no credit for it, a bad request), the turn keeps the
first reply and says which setting to raise.

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
from .output_cap import LENGTH_FINISH, OUTPUT_CAP_THINKING
from .protocol import AssistantDelta, AssistantRetract, DaemonEvent
from .provider import RetryConfig, retry_delay

if TYPE_CHECKING:
    from .session import Session

log = get_logger("tstd.reply_retry")

# The codes a turn withdraws and retries once output has begun.
WITHDRAWN_CODES = frozenset({"stream_stalled"})

# A reply the endpoint cut off partway (``stream_interrupted`` with the
# provider's ``resendable`` flag): a dropped connection, a stream that ended
# inside a tool call or without a finish, an in-band 429 or 5xx (TD-5183).
# A local server that restarts mid-reply, say to lend a GPU, lands here; it
# is back in a minute or two, and ``provider_retry`` waits for it before the
# second ask streams. Before TD-5183 this failed a multi-hour turn outright
# (TD-5152 left the resend to the user). An in-band 4xx is never resendable.
CUT_OFF_CODE = "stream_interrupted"
CUT_OFF_RETRIES = 2

# A reply the reply guard cut (TD-5175, reply_guard.py): junk, a loop,
# leaked tool markup or runaway thinking. Withdrawn and asked again up to
# the tier's ``reply_guard.retries``, apart from the stall allowance.
GUARDED_CODE = "reply_guarded"

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
    # The provider's flag on a reply cut off partway by the endpoint (a
    # dropped connection, an in-band 429 or 5xx), which moves a turn to a
    # backup although it is not retried (TD-5120).
    endpoint_failed: bool = False
    # The reply's plaintext thinking from a ``reasoning`` / ``reasoning_content``
    # stream (TD-5174), kept so a tier with ``echo_reasoning`` can send it back
    # on the tool-call step it led to. Empty when the stream carried none.
    reasoning: str = ""
    # The provider's flag on a cut-off reply the same endpoint may answer if
    # asked again (TD-5183). Withdrawn and asked again like a stall.
    resendable: bool = False
    # The max_tokens this call sent: the tier's cap, or the larger one the
    # retry after a reply that spent it thinking asked with (TD-5233).
    max_tokens: int | None = None


def new_response_id() -> str:
    """A fresh id for one model call's deltas."""
    return f"resp-{uuid.uuid4().hex[:16]}"


def stall_retries(retry: ProviderRetryConfig) -> int:
    """How many times a turn redoes a call that stalled mid-reply."""
    return min(STALL_RETRIES, retry.max_retries)


def cut_off_retries(stall_allowed: int) -> int:
    """How many times a turn redoes a call the endpoint cut off (TD-5183).

    ``provider_retry.max_retries`` 0 turns it off, as it does the stall
    retry; *stall_allowed* is 0 exactly then.
    """
    return CUT_OFF_RETRIES if stall_allowed > 0 else 0


def thinking_ate_cap(result: StreamResult) -> bool:
    """True when *result* ended at the output cap with no answer (TD-5233).

    No text and no tool call, yet the stream stopped at ``max_tokens``:
    the model spent the cap thinking, which most providers count against
    it.
    """
    return (
        not result.failed
        and result.finish_reason == LENGTH_FINISH
        and not result.content.strip()
        and not result.tool_calls
    )


def _stopping(session: Session) -> bool:
    return session.cancel_requested or session.stop_turn_requested


def _stopped(result: StreamResult, session: Session) -> StreamResult:
    """*result* as a call Stop or cancel ended, with nothing kept."""
    return result._replace(
        failed=True,
        content="",
        tool_calls={},
        error_msg="cancelled" if session.cancel_requested else "stopped",
        error_code=None,
        raw_detail="",
        reasoning_details=None,
    )


def _raise_refused(result: StreamResult, session: Session) -> bool:
    """The larger-cap request failed before any output, for good.

    A request error (no credit for the larger reservation, a bad request,
    an overflow) would fail the turn with an error that hides the real
    cause. A failure of the endpoint is not one: the loop's backups take
    it. Stop and cancel end the turn as they always do.
    """
    return result.failed and not result.began and not result.retryable and not _stopping(session)


def _withdrawable(
    result: StreamResult, session: Session, retried: int, allowed: int, guard_retries: int = 0
) -> bool:
    if session.cancel_requested or session.stop_turn_requested:
        return False
    if result.failed and result.error_code == GUARDED_CODE:
        return retried < guard_retries
    if result.failed and result.error_code == CUT_OFF_CODE:
        return result.began and result.resendable and retried < cut_off_retries(allowed)
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
    *,
    guard_retries: int = 0,
    cap_retries: int = 0,
    raised_cap: int | None = None,
) -> StreamResult:
    """Run *attempt* under a fresh response id; withdraw and redo a stall.

    *attempt* streams one model call, stamping its deltas with the id it is
    given. A call that stalls after output began is withdrawn
    (``assistant_retract``) and, after ``provider_retry``'s first backoff,
    run again under a new id. Stop or cancel during the wait ends it with
    nothing kept: the withdrawn text is not the reply.

    A call that spent the output cap thinking is withdrawn and run again
    at once, up to *cap_retries* times; *attempt* sends the larger cap
    (*raised_cap*, named in the log) from then on. If the larger request is
    refused before any output, the first call's result is returned instead;
    a refusal of a later stall or guard retry is returned as itself.
    """
    allowed = stall_retries(retry)
    retried = 0
    cap_retried = 0
    at_cap: StreamResult | None = None
    # Only the call right after the cap retry is the larger request; a
    # refusal of a later stall or guard retry is that call's own error.
    raise_pending = False
    while True:
        response_id = new_response_id()
        result = await attempt(response_id)
        if raise_pending and at_cap is not None and _raise_refused(result, session):
            _log_raise_refused(session, result, at_cap)
            return at_cap
        raise_pending = False
        if thinking_ate_cap(result) and cap_retried < cap_retries and not _stopping(session):
            await _retract(session, response_id, OUTPUT_CAP_THINKING)
            cap_retried += 1
            raise_pending = True
            if at_cap is None:
                at_cap = result
            log.warning(
                "reply spent its output cap thinking; asking again with a larger cap",
                extra={
                    "extra_fields": {
                        "session_id": session.id,
                        "max_tokens": result.max_tokens,
                        "raised_max_tokens": raised_cap,
                        "attempt": cap_retried,
                    }
                },
            )
            if _stopping(session):
                return _stopped(result, session)
            continue
        if not _withdrawable(result, session, retried, allowed, guard_retries):
            return result
        await _retract(session, response_id, result.error_code or "stream_stalled")
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
            "reply withdrawn after a stall or cut-off, asking again",
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
        if _stopping(session):
            return _stopped(result, session)


async def _retract(session: Session, response_id: str, reason: str) -> None:
    await session.event_log.add(
        AssistantRetract(session_id=session.id, response_id=response_id, reason=reason, seq=1)
    )


def _log_raise_refused(session: Session, refused: StreamResult, at_cap: StreamResult) -> None:
    log.warning(
        "the larger output cap was refused; keeping the reply that spent the cap thinking",
        extra={
            "extra_fields": {
                "session_id": session.id,
                "code": refused.error_code,
                "max_tokens": at_cap.max_tokens,
                "raised_max_tokens": refused.max_tokens,
            }
        },
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
