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
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..logging import get_logger
from ..protocol import AssistantDelta, TurnComplete
from ..session import Session
from .models import DeliverTo, Job
from .schedule import advance_job, arm_cadence_job, as_utc, due_jobs
from .store import list_jobs, save_job

log = get_logger("tstd.scheduler.runner")

_TURN_TIMEOUT_SECS = 120.0

TurnFn = Callable[[Path, str], Awaitable[str]]
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
        summary = await run_turn(Path(job.workspace), job.instruction)
    except Exception as exc:
        log.exception(
            "scheduled turn failed",
            extra={"extra_fields": {"job_id": job.id, "error": str(exc)}},
        )
        summary = f"scheduled run failed: {exc}"
    await deliver(job.deliver_to, summary)
    await asyncio.to_thread(save_job, data_dir, advance_job(job, now))


async def run_turn_on_daemon(host: SessionHost, workspace: Path, message: str) -> str:
    """In-process ``tst run``: start a session, one user message, wait."""
    if not await asyncio.to_thread(workspace.is_dir):
        return f"workspace is not a directory: {workspace}"
    raw = await host._start_session(str(workspace))
    if raw is None:
        return "open_workspace did not return a session"
    opened = json.loads(raw)
    session_id = opened.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return "open_workspace did not return a session"
    getter = getattr(host.session_registry, "get", None)
    if getter is None:
        return "daemon has no session registry"
    session = getter(session_id)
    if not isinstance(session, Session):
        return f"session {session_id} was not registered"
    await session.add_user_message(message)
    await _wait_turn_complete(session)
    return turn_summary(session)


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
    parts = [
        event.delta for event in session.event_log.all_events if isinstance(event, AssistantDelta)
    ]
    text = "".join(parts).strip()
    if text:
        return text
    for event in reversed(session.event_log.all_events):
        if isinstance(event, TurnComplete) and event.failed:
            return event.error_code or "turn_failed"
    return ""
