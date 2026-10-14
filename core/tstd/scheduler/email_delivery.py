"""Email delivery for a finished scheduled run (TD-3820).

``deliver`` stays ``(channel, summary)`` so every injected test fake
keeps working. The daemon binds a sender for the tick and for Run now.
When that sender is unset, email falls through to ``deliver`` and the
fake records the channel. A send that fails is stamped on the job and
on the history line. It does not change ``last_status``.

The body of a finished turn is the assistant text after the last tool
call. Notices (late, parked, a missed follow-on) have no transcript,
so they email the same line the other channels already get.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..logging import get_logger
from ..notify.email import EmailNotifyError
from ..protocol import AssistantDelta, DaemonEvent, ToolCall
from .history import set_run_delivery
from .models import DeliverTo, DeliveryStatus, Job, normalize_summary
from .store import transform_job

log = get_logger("tstd.scheduler.email")

DeliverFn = Callable[[DeliverTo, str], Awaitable[None]]


class JobEmailSender(Protocol):
    """One report to the job's address. The daemon closes over its config."""

    async def __call__(self, job: Job, body: str, when: datetime) -> None: ...


_EMAIL_SENDER: ContextVar[JobEmailSender | None] = ContextVar(
    "tstd_job_email_sender",
    default=None,
)


def bind_email_sender(sender: JobEmailSender) -> Token[JobEmailSender | None]:
    """Bind *sender* for this task and for tasks it creates."""
    return _EMAIL_SENDER.set(sender)


def reset_email_sender(token: Token[JobEmailSender | None]) -> None:
    _EMAIL_SENDER.reset(token)


def final_assistant_text(events: list[DaemonEvent]) -> str:
    """Assistant text after the last tool call, or the whole turn if there is none.

    An empty suffix (the model called a tool and then said nothing) falls
    back to the full assistant text so the mail is not blank.
    """
    last_tool = -1
    for index, event in enumerate(events):
        if isinstance(event, ToolCall):
            last_tool = index
    full = _assistant_text(events, 0)
    if last_tool < 0:
        return full
    suffix = _assistant_text(events, last_tool + 1)
    return suffix or full


def _assistant_text(events: list[DaemonEvent], start: int) -> str:
    parts = [event.delta for event in events[start:] if isinstance(event, AssistantDelta)]
    return "".join(parts).strip()


async def deliver_job(
    deliver: DeliverFn,
    data_dir: Path,
    job: Job | None,
    channel: DeliverTo,
    summary: str,
    when: datetime,
    *,
    body: str | None = None,
    started_at: str | None = None,
    job_id: str | None = None,
) -> None:
    """Deliver one receipt. Email uses the bound sender when there is one."""
    if channel != "email" or (job is None and job_id is None):
        await deliver(channel, summary)
        return
    current = job
    if current is None and job_id is not None:
        from .store import get_job

        current = await asyncio.to_thread(get_job, data_dir, job_id)
    if current is None:
        await deliver(channel, summary)
        return
    text = body.strip() if body and body.strip() else summary
    status, error = await _send_email(deliver, current, text, when)
    stamp_id = current.id
    if started_at is not None:
        await asyncio.to_thread(_stamp, data_dir, stamp_id, started_at, status, error)


async def _send_email(
    deliver: DeliverFn,
    job: Job,
    text: str,
    when: datetime,
) -> tuple[DeliveryStatus, str | None]:
    sender = _EMAIL_SENDER.get()
    try:
        if sender is None:
            await deliver("email", text)
        else:
            await sender(job, text, when)
    except EmailNotifyError as exc:
        log.warning(
            "scheduled email delivery failed",
            extra={"extra_fields": {"job_id": job.id, "error_class": exc.error_class}},
        )
        return "failed", f"{exc.error_class}: {exc.message}"[:300]
    except Exception as exc:
        log.warning(
            "scheduled email delivery failed",
            extra={"extra_fields": {"job_id": job.id, "error_class": type(exc).__name__}},
        )
        return "failed", f"{type(exc).__name__}: send failed"
    return "ok", None


def _stamp(
    data_dir: Path,
    job_id: str,
    started_at: str,
    status: DeliveryStatus,
    error: str | None,
) -> None:
    """History line first, then the row. Neither write touches ``last_status``."""
    safe = normalize_summary(error) if error else None
    set_run_delivery(data_dir, job_id, started_at, status, safe)

    def mutate(current: Job) -> Job:
        return current.model_copy(
            update={"last_delivery": status, "last_delivery_error": safe},
        )

    transform_job(data_dir, job_id, mutate)
