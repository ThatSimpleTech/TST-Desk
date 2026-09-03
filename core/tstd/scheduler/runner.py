"""Wake due jobs, run one in-process turn, deliver once (TD-3804).

Execution is the same path ``tst run`` uses — open a workspace session
and enqueue one user message — against the live daemon. It does not
spawn a nested process. Caps and the classifier stay on that session.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..logging import get_logger
from ..protocol import AssistantDelta, TurnComplete
from ..session import Session
from .models import DeliverTo, Job
from .schedule import advance_job, arm_cadence_job, as_utc, due_jobs, record_run
from .store import list_jobs, save_job

log = get_logger("tstd.scheduler.runner")

_TURN_TIMEOUT_SECS = 120.0


@dataclass(frozen=True)
class TurnResult:
    """What one scheduled fire produced (TD-3807).

    ``run_turn`` may still return a bare ``str`` — every injected test fake
    does — which is read as a successful turn with no session to point at.
    """

    summary: str
    ok: bool = True
    session_id: str | None = None


TurnFn = Callable[[Path, str], Awaitable["str | TurnResult"]]
SendFn = Callable[[DeliverTo, str], Awaitable[None]]
WindowFn = Callable[[str], Awaitable[None]]


class Deliver(Protocol):
    """One summary to one channel. Tests inject a mock."""

    async def __call__(self, channel: DeliverTo, summary: str) -> None: ...


class SessionHost(Protocol):
    """The in-process surface ``tst run`` uses: open workspace, enqueue."""

    async def _start_session(self, workspace_path: str) -> str | None: ...

    @property
    def session_registry(self) -> object: ...


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
) -> list[str]:
    """Fire each due job once, deliver once, then advance. Sequential."""
    now_utc = as_utc(now)
    jobs = await asyncio.to_thread(list_jobs, data_dir)
    for job in jobs:
        if job.paused or job.next_run is not None or job.cadence is None:
            continue
        await asyncio.to_thread(save_job, data_dir, arm_cadence_job(job, now_utc))
    jobs = await asyncio.to_thread(list_jobs, data_dir)
    ran: list[str] = []
    for job in due_jobs(jobs, now_utc):
        await _run_one(data_dir, job, now_utc, run_turn, deliver)
        ran.append(job.id)
    return ran


async def _run_one(
    data_dir: Path,
    job: Job,
    now: datetime,
    run_turn: TurnFn,
    deliver: Deliver,
) -> None:
    try:
        outcome = await run_turn(Path(job.workspace), job.instruction)
        result = outcome if isinstance(outcome, TurnResult) else TurnResult(summary=outcome)
    except Exception as exc:
        log.exception(
            "scheduled turn failed",
            extra={"extra_fields": {"job_id": job.id, "error": str(exc)}},
        )
        result = TurnResult(summary=f"scheduled run failed: {exc}", ok=False)
    await deliver(job.deliver_to, result.summary)
    # Advance first, then stamp the receipt on the advanced copy, so the
    # saved row carries both the next slot and what just happened.
    stamped = record_run(
        advance_job(job, now),
        now,
        status="ok" if result.ok else "failed",
        summary=result.summary,
        session_id=result.session_id,
    )
    await asyncio.to_thread(save_job, data_dir, stamped)


async def run_turn_on_daemon(host: SessionHost, workspace: Path, message: str) -> TurnResult:
    """In-process ``tst run``: start a session, one user message, wait.

    Every early return is a failure the user needs to see on the job row —
    a workspace that has been moved or deleted is the common one.
    """
    if not await asyncio.to_thread(workspace.is_dir):
        return TurnResult(f"workspace is not a directory: {workspace}", ok=False)
    raw = await host._start_session(str(workspace))
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
    await _wait_turn_complete(session)
    summary, failed = turn_outcome(session)
    return TurnResult(summary, ok=not failed, session_id=session_id)


async def _wait_turn_complete(session: Session) -> None:
    deadline = time.monotonic() + _TURN_TIMEOUT_SECS
    seen = session.event_log.last_seq
    while time.monotonic() < deadline:
        if any(isinstance(event, TurnComplete) for event in session.event_log.all_events):
            return
        remaining = max(0.05, deadline - time.monotonic())
        try:
            seen = await asyncio.wait_for(
                session.event_log.wait_for_new_event(seen),
                timeout=min(remaining, 0.25),
            )
        except TimeoutError:
            continue
    raise TimeoutError("timed out waiting for turn_complete")


def turn_summary(session: Session) -> str:
    """Assistant text from the turn, or the failed ``error_code``."""
    return turn_outcome(session)[0]


def turn_outcome(session: Session) -> tuple[str, bool]:
    """``(summary, failed)`` for the turn just finished.

    A turn that ends with ``turn_complete.failed`` is a failure even though
    it produced a summary, and a turn that produced no text at all is one
    too — "it ran and said nothing" is not something to report as success.
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
        return text, failed_code is not None
    if failed_code is not None:
        return failed_code, True
    return "the scheduled turn produced no output", True
