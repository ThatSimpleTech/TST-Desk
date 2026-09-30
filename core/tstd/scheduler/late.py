"""Skip a due slot that is already past its grace (TD-3813).

This does not take a turn callback. A missed morning digest must not
open a session: there is nothing to run, and a callback here would be
a way to start one. Run now does not call this. The caller still counts
the id, so an open pane hears the receipt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from ..logging import get_logger
from .grace import past_grace, skip_line
from .history import append_run
from .models import DeliverTo, Job
from .schedule import advance_job, parse_next_run, record_run
from .store import save_job

log = get_logger("tstd.scheduler.late")

SlotDeliver = Callable[[DeliverTo, str], Awaitable[None]]


async def skip_if_late(
    data_dir: Path,
    job: Job,
    now: datetime,
    deliver: SlotDeliver,
) -> bool:
    """Advance, record ``missed``, and deliver one line. True if skipped.

    Save happens before the notify, same as a real fire: an overdue
    ``next_run`` left on disk would skip again on the next tick.
    """
    slot_raw = job.next_run
    if slot_raw is None:
        return False
    slot = parse_next_run(slot_raw)
    if not past_grace(slot, job.grace, now):
        return False
    line = skip_line(slot, job.timezone, now)
    # A skip is the regular slot ending. A leftover retry counter would
    # make the next fire look like a retry and skip grace the other way.
    stamped = record_run(
        advance_job(job, now).model_copy(update={"attempt": 0, "resume_at": None}),
        now,
        status="missed",
        summary=line,
        session_id=None,
    )
    await asyncio.to_thread(save_job, data_dir, stamped)
    log.info(
        "scheduled slot skipped, past grace",
        extra={"extra_fields": {"job_id": job.id}},
    )
    await asyncio.to_thread(
        append_run,
        data_dir,
        job.id,
        started_at=now,
        scheduled_for=slot_raw,
        trigger="schedule",
        status="missed",
        summary=line,
        session_id=None,
    )
    await deliver(job.deliver_to, line)
    return True
