"""Wake due jobs, run one in-process turn, deliver once (TD-3804).

Execution is the same path ``tst run`` uses — open a workspace session
and enqueue one user message — against the live daemon. It does not
spawn a nested process. Caps and the classifier stay on that session.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from ..config import ModelConfig
from ..logging import get_logger
from ..protocol import AssistantDelta, TurnComplete
from ..session import Session
from .calendar import begin_tick, consider_calendar, note_unreadable, skip_for_calendar
from .email_delivery import deliver_job, final_assistant_text
from .history import JobRun, append_run
from .late import skip_if_late
from .limit import MAX_RUN_ERROR_CODE, run_limit_seconds, stop_summary
from .models import DeliverTo, Job
from .park import (
    ParkHook,
    await_turn_or_approval,
    park_run,
    pending_tool_summary,
    skip_if_parked,
)
from .pin import (
    bind_scheduled_pin,
    current_scheduled_pin,
    reset_scheduled_pin,
    scheduled_run_block,
)
from .retry import failure_reason, settle_scheduled
from .schedule import arm_cadence_job, as_utc, due_jobs, record_run
from .store import get_job, list_jobs, save_job

log = get_logger("tstd.scheduler.runner")


@dataclass(frozen=True)
class TurnResult:
    """What one scheduled fire produced (TD-3807).

    ``run_turn`` may still return a bare ``str`` — every injected test fake
    does — which is read as a successful turn with no session to point at.
    """

    summary: str
    ok: bool = True
    session_id: str | None = None
    # Typed cause when ``ok`` is false (``connection_error``, ``auth_failed``).
    # The summary is for the receipt; the code is what a retry decision reads,
    # because the prose does not say ``context_overflow``.
    error_code: str | None = None
    # The session is on an approval card. Not a failure, and not a retry:
    # the runner returns without waiting out the turn budget (TD-3815).
    waiting: bool = False
    # Assistant text after the last tool call (TD-3820). None on a fake
    # that returned a string, and on a turn that produced no assistant
    # text. Email uses this; the receipt stays ``summary``.
    report: str | None = None


TurnFn = Callable[[Path, str], Awaitable["str | TurnResult"]]
SendFn = Callable[[DeliverTo, str], Awaitable[None]]
WindowFn = Callable[[str], Awaitable[None]]


class InFlight:
    """Job ids with a turn underway. The tick and Run now share one.

    Both callers sit on the daemon's loop, and the turn is the await that
    lets the other in. The check and the insert have to be the same
    synchronous call: a gap would let two runs of one job overlap, and
    waiting out the overlap would stall the tick, which is inline.
    """

    def __init__(self) -> None:
        self._ids: set[str] = set()

    def __contains__(self, job_id: object) -> bool:
        return job_id in self._ids

    def claim(self, job_id: str) -> bool:
        """Own ``job_id``. False when a turn for it is already underway."""
        if job_id in self._ids:
            return False
        self._ids.add(job_id)
        return True

    def release(self, job_id: str) -> None:
        self._ids.discard(job_id)


class Deliver(Protocol):
    """One summary to one channel. Tests inject a mock."""

    async def __call__(self, channel: DeliverTo, summary: str) -> None: ...


class SessionHost(Protocol):
    """The in-process surface ``tst run`` uses: open workspace, enqueue."""

    config: ModelConfig

    async def _start_session(
        self,
        workspace_path: str,
        *,
        preset: str | None = None,
        engine: Literal["native", "grok"] | None = None,
    ) -> str | None: ...

    @property
    def session_registry(self) -> object: ...


async def channel_notify(config: ModelConfig, channel: DeliverTo, summary: str) -> None:
    """Production Slack/ntfy POST. Window delivery stays on RecordingDeliver.

    ``Daemon`` uses this when no test injects ``notify_send``. Operational
    failures stay inside ``notify.*.send`` (logged, never raised, URL redacted).
    """
    if channel == "slack":
        from ..notify.slack import send as slack_send

        await slack_send(config, summary)
        return
    if channel == "ntfy":
        from ..notify.ntfy import send as ntfy_send

        await ntfy_send(config, summary)


class RecordingDeliver:
    """Window records a callback; slack/ntfy call an optional send hook."""

    def __init__(
        self,
        *,
        send: SendFn | None = None,
        on_window: WindowFn | None = None,
    ) -> None:
        self.send = send
        self.on_window = on_window
        self.records: list[tuple[DeliverTo, str]] = []

    async def __call__(self, channel: DeliverTo, summary: str) -> None:
        self.records.append((channel, summary))
        if channel == "window":
            if self.on_window is not None:
                await self.on_window(summary)
            else:
                log.info(
                    "scheduled delivery to window",
                    extra={"extra_fields": {"summary_len": len(summary)}},
                )
            return
        if self.send is not None:
            await self.send(channel, summary)
            return
        log.info(
            "scheduled delivery skipped (no send hook)",
            extra={"extra_fields": {"channel": channel}},
        )


async def run_due_jobs(
    data_dir: Path,
    now: datetime,
    *,
    run_turn: TurnFn,
    deliver: Deliver,
    in_flight: InFlight | None = None,
    on_parked: ParkHook | None = None,
) -> list[str]:
    """Fire each due job once, deliver once, then advance. Sequential.

    ``in_flight`` is the guard Run now claims too. A due id already in it
    is left due: the tick must not wait, or one manual turn would hold
    every other job (and shutdown) until it returned.
    """
    # One read of each calendar for this tick, even if the file changes
    # while the jobs run. The next tick looks at the mtime again.
    begin_tick()
    now_utc = as_utc(now)
    jobs = await asyncio.to_thread(list_jobs, data_dir)
    for job in jobs:
        if job.paused or job.next_run is not None or job.cadence is None:
            continue
        await asyncio.to_thread(save_job, data_dir, arm_cadence_job(job, now_utc))
    jobs = await asyncio.to_thread(list_jobs, data_dir)
    ran: list[str] = []
    for job in due_jobs(jobs, now_utc):
        if in_flight is not None and not in_flight.claim(job.id):
            continue
        try:
            await _run_one(
                data_dir,
                job,
                now_utc,
                run_turn,
                deliver,
                on_parked=on_parked,
                in_flight=in_flight,
            )
        finally:
            if in_flight is not None:
                in_flight.release(job.id)
        ran.append(job.id)
    return ran


async def begin_manual_run(data_dir: Path, job_id: str, in_flight: InFlight) -> Job | str:
    """Claim ``job_id`` for Run now, or an error code the handler can send.

    The claim is synchronous and happens before the caller schedules the
    turn, so the ``job_list`` it replies with already shows the row running.
    """
    job = await asyncio.to_thread(get_job, data_dir, job_id)
    if job is None:
        return "job_not_found"
    if not in_flight.claim(job.id):
        return "job_running"
    return job


async def run_manual_job(
    data_dir: Path,
    job: Job,
    now: datetime,
    *,
    run_turn: TurnFn,
    deliver: Deliver,
    on_parked: ParkHook | None = None,
    in_flight: InFlight | None = None,
) -> None:
    """One explicit fire. Receipt and delivery, without touching the schedule.

    ``advance_job`` is what spends a one-shot and moves the 7:45 slot.
    Run now is not that: the row is re-read after the turn so a pause or
    an edit made while it ran is what receives the receipt, and a job
    deleted in that window is not written back into existence. A park
    is the same shape: the schedule stays, and the watch is armed only
    after the row is on disk.
    """
    result = await _turn_result(job, run_turn)
    fresh = await asyncio.to_thread(get_job, data_dir, job.id)
    channel = job.deliver_to
    if fresh is not None:
        channel = fresh.deliver_to
        if result.waiting and result.session_id:
            await _park_and_arm(
                data_dir,
                fresh,
                now,
                result,
                advance=False,
                trigger="manual",
                scheduled_for=None,
                deliver=deliver,
                on_parked=on_parked,
            )
            return
        if result.waiting:
            result = TurnResult(summary=result.summary or "a tool call", ok=False)
        stamped = record_run(
            fresh,
            now,
            status="ok" if result.ok else "failed",
            summary=result.summary,
            session_id=result.session_id,
        )
        await asyncio.to_thread(save_job, data_dir, stamped)
        # A job deleted during the turn is not written back, and neither is
        # a history line for it — delete already removed the file.
        remembered = await _remember_run(
            data_dir,
            fresh.id,
            now,
            result,
            trigger="manual",
            scheduled_for=None,
        )
        linked = stamped
    else:
        remembered = None
        linked = None
    await deliver_job(
        deliver,
        data_dir,
        fresh,
        channel,
        result.summary,
        now,
        body=result.report,
        started_at=None if remembered is None else remembered.started_at,
    )
    if linked is not None and result.ok:
        await _start_chain(
            data_dir,
            linked,
            now,
            run_turn=run_turn,
            deliver=deliver,
            in_flight=in_flight,
            on_parked=on_parked,
        )


async def _run_one(
    data_dir: Path,
    job: Job,
    now: datetime,
    run_turn: TurnFn,
    deliver: Deliver,
    *,
    on_parked: ParkHook | None = None,
    in_flight: InFlight | None = None,
) -> None:
    # A parked approval still owns the previous session. The next slot
    # is missed before grace, which would otherwise describe the same
    # instant as late and open nothing for a different reason.
    if await skip_if_parked(data_dir, job, now, deliver):
        return
    fresh = await asyncio.to_thread(get_job, data_dir, job.id)
    if fresh is None:
        return
    job = fresh
    # Grace is the regular slot only. A retry is already that slot's
    # second chance; skipping it would drop the try the user asked for.
    # Run now never enters this function, so asking for a run cannot skip.
    if job.attempt == 0 and await skip_if_late(data_dir, job, now, deliver):
        return
    # The calendar is the regular slot only. A retry is that slot's later
    # try, and Run now never enters this function. A blocked day is not an
    # ok end, so the follow-on does not start. An unreadable file is not
    # a block: the turn still runs, and the receipt says so once.
    verdict = await consider_calendar(job)
    if job.attempt == 0 and verdict == "skip":
        await skip_for_calendar(data_dir, job, now)
        return
    result = await _turn_result(job, run_turn)
    if verdict == "unreadable":
        result = replace(result, summary=note_unreadable(result.summary))
    if result.waiting and result.session_id:
        # Not settle_scheduled: a waiting card is not a failure, so it
        # must not arm a retry, and the slot still advances once.
        await _park_and_arm(
            data_dir,
            job,
            now,
            result,
            advance=True,
            trigger="schedule",
            scheduled_for=job.next_run,
            deliver=deliver,
            on_parked=on_parked,
        )
        return
    if result.waiting:
        result = TurnResult(summary=result.summary or "a tool call", ok=False)
    # The instant that just came due. A retry's next_run is that instant,
    # not the morning slot it is standing in for.
    fired = job.next_run
    settled = settle_scheduled(
        job,
        now,
        ok=result.ok,
        reason=failure_reason(ok=result.ok, error_code=result.error_code, summary=result.summary),
        summary=result.summary,
    )
    # Stamp the next slot (and the run receipt) before notify. Deliver-first
    # left an overdue ``next_run`` on disk if the test (or a crash) observed
    # the record before ``save_job`` finished — a second tick would fire again.
    # An intermediate failure still stamps the receipt, and does not deliver:
    # the channel hears the slot once, when it succeeds or the tries run out.
    stamped = record_run(
        job.model_copy(update=settled.updates),
        now,
        status="ok" if result.ok else "failed",
        summary=settled.receipt,
        session_id=result.session_id,
    )
    await asyncio.to_thread(save_job, data_dir, stamped)
    remembered = await _remember_run(
        data_dir,
        job.id,
        now,
        result,
        trigger="schedule",
        scheduled_for=fired,
        attempt=settled.attempt,
        attempts=settled.attempts,
    )
    if not settled.deliver:
        log.info(
            "scheduled run will retry",
            extra={"extra_fields": {"job_id": job.id, "attempt": settled.attempt}},
        )
        return
    await deliver_job(
        deliver,
        data_dir,
        stamped,
        job.deliver_to,
        settled.receipt,
        now,
        body=result.report,
        started_at=remembered.started_at,
    )
    if result.ok:
        await _start_chain(
            data_dir,
            stamped,
            now,
            run_turn=run_turn,
            deliver=deliver,
            in_flight=in_flight,
            on_parked=on_parked,
        )


async def _park_and_arm(
    data_dir: Path,
    job: Job,
    now: datetime,
    result: TurnResult,
    *,
    advance: bool,
    trigger: Literal["schedule", "manual"],
    scheduled_for: str | None,
    deliver: Deliver,
    on_parked: ParkHook | None,
) -> None:
    """Save the park, deliver the waiting line, then arm the watch."""
    session_id = result.session_id
    if session_id is None:
        return
    started = await park_run(
        data_dir,
        job,
        now,
        session_id=session_id,
        summary=result.summary,
        advance=advance,
        trigger=trigger,
        scheduled_for=scheduled_for,
        deliver=deliver,
    )
    if on_parked is not None:
        on_parked(job.id, session_id, started)


async def _start_chain(
    data_dir: Path,
    parent: Job,
    now: datetime,
    *,
    run_turn: TurnFn,
    deliver: Deliver,
    in_flight: InFlight | None,
    on_parked: ParkHook | None,
) -> None:
    """After an ok fire, start ``then`` if the job has one.

    Imported here so loading the runner does not load the chain, which
    imports this module for the turn.
    """
    from .chain import follow_chain

    await follow_chain(
        data_dir,
        parent,
        now,
        depth=0,
        run_turn=run_turn,
        deliver=deliver,
        in_flight=in_flight,
        on_parked=on_parked,
    )


async def _remember_run(
    data_dir: Path,
    job_id: str,
    now: datetime,
    result: TurnResult,
    *,
    trigger: Literal["schedule", "manual"],
    scheduled_for: str | None,
    attempt: int | None = None,
    attempts: int | None = None,
) -> JobRun:
    """Append the fire off the event loop. ``last_*`` stays the row summary."""
    status: Literal["ok", "failed"] = "ok" if result.ok else "failed"
    return await asyncio.to_thread(
        append_run,
        data_dir,
        job_id,
        started_at=now,
        scheduled_for=scheduled_for,
        trigger=trigger,
        status=status,
        summary=result.summary,
        session_id=result.session_id,
        attempt=attempt,
        attempts=attempts,
    )


async def _turn_result(job: Job, run_turn: TurnFn) -> TurnResult:
    # The callback stays (workspace, instruction). The pin is task-local so
    # the session opens on the job's model, and the next job on this task
    # does not inherit it. Widening TurnFn would break every test fake.
    token = bind_scheduled_pin(job)
    try:
        try:
            outcome = await run_turn(Path(job.workspace), job.instruction)
        except TimeoutError as exc:
            # The wait for turn_complete, not a provider status. It is the
            # same kind of blip as a read timeout: try the slot again.
            log.exception(
                "scheduled turn timed out",
                extra={"extra_fields": {"job_id": job.id}},
            )
            return TurnResult(
                summary=f"scheduled run failed: {exc}",
                ok=False,
                error_code="timeout",
            )
        except Exception as exc:
            log.exception(
                "scheduled turn failed",
                extra={"extra_fields": {"job_id": job.id, "error": str(exc)}},
            )
            return TurnResult(summary=f"scheduled run failed: {exc}", ok=False)
        if isinstance(outcome, TurnResult):
            return outcome
        return TurnResult(summary=outcome)
    finally:
        reset_scheduled_pin(token)


async def run_turn_on_daemon(host: SessionHost, workspace: Path, message: str) -> TurnResult:
    """In-process ``tst run``: start a session, one user message, wait.

    Every early return is a failure the user needs to see on the job row —
    a workspace that has been moved or deleted is the common one. A pinned
    preset that has left the catalog is the same kind of failure: a receipt,
    not a silent switch to the window's model.
    """
    blocked = await scheduled_run_block(host.config.presets, grok_binary=host.config.engine.binary)
    if blocked is not None:
        return TurnResult(blocked, ok=False, error_code=_pin_block_code(blocked))
    if not await asyncio.to_thread(workspace.is_dir):
        return TurnResult(
            f"workspace is not a directory: {workspace}",
            ok=False,
            error_code="workspace_missing",
        )
    pin = current_scheduled_pin()
    raw = await host._start_session(str(workspace), preset=pin.preset, engine=pin.engine)
    if raw is None:
        return TurnResult("open_workspace did not return a session", ok=False)
    opened = json.loads(raw)
    session_id = opened.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return TurnResult("open_workspace did not return a session", ok=False)
    getter = getattr(host.session_registry, "get", None)
    if getter is None:
        return TurnResult("daemon has no session registry", ok=False, session_id=session_id)
    session = getter(session_id)
    if not isinstance(session, Session):
        return TurnResult(
            f"session {session_id} was not registered", ok=False, session_id=session_id
        )
    await session.add_user_message(message)
    # The job's own limit wins. Otherwise the config. An approval returns
    # before the budget; hitting it cancels the turn instead of leaving
    # the loop running after the waiter has given up.
    budget = run_limit_seconds(
        current_scheduled_pin().max_run,
        float(host.config.scheduler.max_run_seconds),
    )
    try:
        parked = await await_turn_or_approval(session, budget)
    except TimeoutError:
        await _stop_over_limit(host, session_id, session)
        log.info(
            "scheduled run stopped at max run time",
            extra={"extra_fields": {"session_id": session_id, "limit_seconds": budget}},
        )
        return TurnResult(
            stop_summary(budget),
            ok=False,
            session_id=session_id,
            error_code=MAX_RUN_ERROR_CODE,
        )
    if parked:
        return TurnResult(
            pending_tool_summary(session),
            ok=True,
            session_id=session_id,
            waiting=True,
        )
    summary, failed, code = turn_outcome(session)
    report = final_assistant_text(session.event_log.all_events) or None
    return TurnResult(
        summary,
        ok=not failed,
        session_id=session_id,
        error_code=code,
        report=report,
    )


async def _stop_over_limit(host: SessionHost, session_id: str, session: Session) -> None:
    """Cancel the session the limit cut, including its loop task.

    ``session.cancel`` alone sets the flag. The runner task is what is
    still inside the provider call, so the registry cancel is the one
    that actually stops it. A host with no registry still flags the session.
    """
    cancel = getattr(host.session_registry, "cancel", None)
    if cancel is not None:
        await cancel(session_id)
        return
    await session.cancel()


def turn_summary(session: Session) -> str:
    """Assistant text from the turn, or the failed ``error_code``."""
    return turn_outcome(session)[0]


def turn_outcome(session: Session) -> tuple[str, bool, str | None]:
    """``(summary, failed, error_code)`` for the turn just finished.

    A turn that ends with ``turn_complete.failed`` is a failure even though
    it produced a summary, and a turn that produced no text at all is one
    too — "it ran and said nothing" is not something to report as success.
    The code is separate from the summary so a retry can tell a down
    provider from a rejected key without reading the sentence.
    """
    failed_code: str | None = None
    for event in reversed(session.event_log.all_events):
        if isinstance(event, TurnComplete) and event.failed:
            failed_code = event.error_code or "turn_failed"
            break
    parts = [
        event.delta for event in session.event_log.all_events if isinstance(event, AssistantDelta)
    ]
    text = "".join(parts).strip()
    if text:
        return text, failed_code is not None, failed_code
    if failed_code is not None:
        return failed_code, True, failed_code
    return "the scheduled turn produced no output", True, None


def _pin_block_code(summary: str) -> str:
    """Stable code for a pin that cannot start. The summary stays the receipt."""
    if summary == "grok engine is unavailable":
        return "engine_unavailable"
    return "preset_missing"
