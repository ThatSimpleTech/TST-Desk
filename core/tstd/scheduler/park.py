"""Park a scheduled run that is waiting on an approval card (TD-3815).

The runner used to sit until the turn budget ended, then record a
timeout. That string is transient, so the next try opened another
session onto the same card. Parking returns as soon as the session is
``awaiting_approval``, advances the slot the way a finished fire does,
and lets one background watch settle the same history line when the
user answers — or a restart, if the process is gone first. A budget
that does expire is a max-run stop: the runner cancels the turn and
does not retry it.

The watch is cancelled on shutdown without settling. The row stays
parked on disk so the next start can close it. Cancelling the session
is the user path and does settle.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..logging import get_logger
from ..protocol import ApprovalRequest, ToolResult, TurnComplete
from ..session import Session
from .email_delivery import deliver_job
from .grace import _clock
from .history import RunTrigger, append_run, close_waiting_run
from .models import DeliverTo, Job, normalize_next_run, normalize_summary
from .retry import finish_slot
from .schedule import as_utc, parse_next_run, record_run
from .store import list_jobs, save_job, transform_job

log = get_logger("tstd.scheduler.park")

# The channel and the history line use these sentences unchanged so a
# retry classifier cannot mistake them for a timeout.
PARKED_MISS = "previous run still waiting for approval"
UNANSWERED = "approval never answered"
_FALLBACK_SUMMARY = "a tool call"

SlotDeliver = Callable[[DeliverTo, str], Awaitable[None]]
ParkHook = Callable[[str, str, str], None]
_Outcome = Literal["ok", "failed"]


@dataclass(frozen=True)
class _Disk:
    saved: bool
    deliver: bool
    channel: DeliverTo | None
    summary: str


def pending_tool_summary(session: Session) -> str:
    """The latest approval card's summary, redacted, or a plain fallback."""
    found = ""
    for event in session.event_log.all_events:
        if isinstance(event, ApprovalRequest):
            text = event.summary.strip()
            if text:
                found = text
    return _safe_summary(found)


async def await_turn_or_approval(session: Session, budget: float) -> bool:
    """True when the session is parked on an approval card.

    A finished turn returns False so the caller reads the outcome.
    ``budget`` is the runner's turn budget. Exceeding it raises
    ``TimeoutError``. The runner cancels that turn and records a
    max-run stop, which is not a transient retry. A turn that never
    asks for approval still uses that budget.
    """
    deadline = time.monotonic() + budget
    seen = session.event_log.last_seq
    while True:
        # A turn that already finished is not parked, even if it passed
        # through an approval the poll did not see in time.
        if _turn_done(session):
            return False
        if session.state == "awaiting_approval":
            return True
        if time.monotonic() >= deadline:
            break
        remaining = deadline - time.monotonic()
        try:
            seen = await asyncio.wait_for(
                session.event_log.wait_for_new_event(seen),
                timeout=min(max(remaining, 0.0), 0.25),
            )
        except TimeoutError:
            continue
    if _turn_done(session):
        return False
    if session.state == "awaiting_approval":
        return True
    raise TimeoutError("timed out waiting for turn_complete")


def waiting_line(timezone: str | None, slot: str | None, summary: str) -> str:
    """One sentence for the channel. The receipt keeps the tool summary.

    The clock matches a skipped slot (``7:45 AM``). Run now has no slot.
    """
    if slot is None:
        return f"This job is waiting for your approval: {summary}"
    clock = _clock(parse_next_run(slot), timezone)
    return f"{clock} job is waiting for your approval: {summary}"


async def park_run(
    data_dir: Path,
    job: Job,
    now: datetime,
    *,
    session_id: str,
    summary: str,
    advance: bool,
    trigger: RunTrigger,
    scheduled_for: str | None,
    deliver: SlotDeliver,
    note: str | None = None,
) -> str:
    """Persist ``waiting``, append the history line, deliver once.

    Returns the history ``started_at``. The caller arms the watch after
    this returns: a crash in between leaves the row parked for revive.
    ``advance`` moves the regular slot. Run now passes False and does
    not touch ``next_run``, ``paused``, ``attempt``, or ``resume_at``.
    """
    stamp = normalize_next_run(as_utc(now).isoformat())
    text = _safe_summary(summary)
    updates = finish_slot(job, now) if advance else {}
    base = job.model_copy(update=updates) if updates else job
    stamped = record_run(
        base,
        now,
        status="waiting",
        summary=text,
        session_id=session_id,
    )
    stamped = stamped.model_copy(
        update={
            "last_run": stamp,
            "parked_session_id": session_id,
            "parked_started_at": stamp,
        }
    )
    await asyncio.to_thread(save_job, data_dir, stamped)
    await asyncio.to_thread(
        append_run,
        data_dir,
        job.id,
        started_at=stamp,
        scheduled_for=scheduled_for,
        trigger=trigger,
        status="waiting",
        summary=text,
        session_id=session_id,
        note=note,
    )
    log.info(
        "scheduled run parked for approval",
        extra={"extra_fields": {"job_id": job.id, "summary_len": len(text)}},
    )
    notice = waiting_line(job.timezone, scheduled_for, text)
    await deliver_job(
        deliver,
        data_dir,
        job,
        job.deliver_to,
        notice,
        now,
        body=notice,
        started_at=stamp,
    )
    return stamp


async def skip_if_parked(
    data_dir: Path,
    job: Job,
    now: datetime,
    deliver: SlotDeliver,
) -> bool:
    """Record a missed slot while a previous run is still parked.

    True when this slot did not start a session. The parked receipt
    stays, so the row keeps saying it is waiting. The cadence still
    advances. Checked before grace: a parked job is not also late.
    """
    found: list[tuple[str | None, DeliverTo]] = []

    def mutate(current: Job) -> Job | None:
        if not current.parked_session_id:
            return None
        found.append((current.next_run, current.deliver_to))
        return current.model_copy(update=finish_slot(current, now))

    saved = await asyncio.to_thread(transform_job, data_dir, job.id, mutate)
    if saved is None or not found:
        return False
    slot, channel = found[-1]
    recorded = await asyncio.to_thread(
        append_run,
        data_dir,
        job.id,
        started_at=now,
        scheduled_for=slot,
        trigger="schedule",
        status="missed",
        summary=PARKED_MISS,
        session_id=None,
    )
    log.info(
        "scheduled slot missed, previous run still waiting",
        extra={"extra_fields": {"job_id": job.id}},
    )
    await deliver_job(
        deliver,
        data_dir,
        job,
        channel,
        PARKED_MISS,
        now,
        body=PARKED_MISS,
        started_at=recorded.started_at,
    )
    return True


async def follow_parked(
    data_dir: Path,
    job_id: str,
    session: Session,
    started_at: str,
    deliver: SlotDeliver,
) -> bool:
    """Wait until the parked turn ends, then settle it.

    ``CancelledError`` propagates. Shutdown cancels this task and must
    leave the park on disk; cancelling the session is a different signal
    and is handled in ``parked_outcome``.
    """
    await wait_for_parked_turn(session)
    status, summary = parked_outcome(session)
    return await _settle(
        data_dir,
        job_id,
        session_id=session.id,
        started_at=started_at,
        status=status,
        summary=summary,
        deliver=deliver,
        trigger="schedule",
        scheduled_for=None,
    )


async def drop_parked(
    data_dir: Path,
    job_id: str,
    session_id: str,
    started_at: str,
    deliver: SlotDeliver,
) -> bool:
    """The session is already gone. The park cannot be answered."""
    return await _settle(
        data_dir,
        job_id,
        session_id=session_id,
        started_at=started_at,
        status="failed",
        summary=UNANSWERED,
        deliver=deliver,
        trigger="schedule",
        scheduled_for=None,
    )


async def revive_parked(data_dir: Path, deliver: SlotDeliver) -> int:
    """Fail every park this process did not open. Returns how many closed.

    Called once, before the first tick. A session restored by session
    revive is not reattached: the approval was never answered here.
    Idempotent: a line that is no longer waiting is not rewritten, and
    a job with no park link is skipped.
    """
    jobs = await asyncio.to_thread(list_jobs, data_dir)
    closed = 0
    for job in jobs:
        session_id = job.parked_session_id
        if not session_id:
            continue
        started = job.parked_started_at or job.last_run or ""
        if await _settle(
            data_dir,
            job.id,
            session_id=session_id,
            started_at=started,
            status="failed",
            summary=UNANSWERED,
            deliver=deliver,
            trigger="schedule",
            scheduled_for=None,
        ):
            closed += 1
    if closed:
        log.info(
            "scheduled parks closed on startup",
            extra={"extra_fields": {"jobs": closed}},
        )
    return closed


async def wait_for_parked_turn(session: Session) -> None:
    """Return when the turn finishes or the session is cancelled.

    The state is checked before the wait. A cancel that already happened
    does not sit on an event that will never arrive.
    """
    seen = session.event_log.last_seq
    while not _park_done(session):
        seen = await session.event_log.wait_for_new_event(seen)


def parked_outcome(session: Session) -> tuple[_Outcome, str]:
    """The receipt for a parked turn that has now ended.

    Cancel wins over a tool result: the user did not answer. A denial
    is that tool result's text, not whatever the model says afterwards.
    """
    if session.state == "cancelled" or session.cancel_requested:
        return "failed", UNANSWERED
    for event in session.event_log.all_events:
        if isinstance(event, ToolResult) and event.error_code == "approval_denied":
            text = event.output.strip()
            return "failed", _safe_summary(text) if text else "Denied by user"
    # Local import: the runner imports this module to park.
    from .runner import turn_outcome

    summary, failed, _code = turn_outcome(session)
    text = _safe_summary(summary)
    return ("failed" if failed else "ok"), text


def _turn_done(session: Session) -> bool:
    return any(isinstance(event, TurnComplete) for event in session.event_log.all_events)


def _park_done(session: Session) -> bool:
    if session.state == "cancelled" or session.cancel_requested:
        return True
    return _turn_done(session)


def _safe_summary(raw: str | None) -> str:
    text = normalize_summary(raw)
    return text or _FALLBACK_SUMMARY


async def _settle(
    data_dir: Path,
    job_id: str,
    *,
    session_id: str,
    started_at: str,
    status: _Outcome,
    summary: str,
    deliver: SlotDeliver,
    trigger: RunTrigger,
    scheduled_for: str | None,
) -> bool:
    """History first, then the job, then one delivery if we still own it.

    Owning it means ``parked_session_id`` is still this session. A newer
    park (Run now) keeps its link; the old history line still settles.
    Delivery happens only when that line was still waiting, so a crash
    between the history rewrite and this save does not send the line twice.
    """
    disk = await asyncio.to_thread(
        _close_and_save,
        data_dir,
        job_id,
        session_id=session_id,
        started_at=started_at,
        status=status,
        summary=summary,
        trigger=trigger,
        scheduled_for=scheduled_for,
    )
    if disk.deliver and disk.channel is not None:
        await deliver_job(
            deliver,
            data_dir,
            None,
            disk.channel,
            disk.summary,
            datetime.now(UTC),
            body=disk.summary,
            started_at=started_at,
            job_id=job_id,
        )
    return disk.saved


def _close_and_save(
    data_dir: Path,
    job_id: str,
    *,
    session_id: str,
    started_at: str,
    status: _Outcome,
    summary: str,
    trigger: RunTrigger,
    scheduled_for: str | None,
) -> _Disk:
    closed = close_waiting_run(
        data_dir,
        job_id,
        started_at=started_at,
        session_id=session_id,
        status=status,
        summary=summary,
        trigger=trigger,
        scheduled_for=scheduled_for,
    )
    final_status = closed.status or status
    final_summary = closed.summary if closed.summary is not None else summary
    channel: DeliverTo | None = None
    saved = False

    def mutate(current: Job) -> Job | None:
        nonlocal channel, saved
        if current.parked_session_id != session_id:
            return None
        saved = True
        channel = current.deliver_to
        updates: dict[str, Any] = {
            "parked_session_id": None,
            "parked_started_at": None,
        }
        if current.last_status == "waiting" and current.last_session_id == session_id:
            updates["last_status"] = final_status
            updates["last_summary"] = normalize_summary(final_summary)
        return current.model_copy(update=updates)

    transform_job(data_dir, job_id, mutate)
    return _Disk(
        saved=saved,
        deliver=saved and closed.flipped,
        channel=channel,
        summary=final_summary,
    )
