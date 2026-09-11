"""When a job is due and how the cadence advances (TD-3804).

A missed ``next_run`` is one fire, then the next slot is computed from
*now* — never a catch-up loop over every skipped interval.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from .models import Job, JobError, RunStatus, normalize_next_run, normalize_summary

_INTERVAL = re.compile(
    r"^every\s+([1-9]\d*)\s+(minutes?|hours?|days?)$",
    re.IGNORECASE,
)
_UNITS = {
    "minute": "minute",
    "minutes": "minute",
    "hour": "hour",
    "hours": "hour",
    "day": "day",
    "days": "day",
}
_CRON_HORIZON = timedelta(days=366)


def as_utc(value: datetime) -> datetime:
    """Timezone-aware UTC. Naive values are treated as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_next_run(raw: str) -> datetime:
    """Parse a stored ISO-8601 ``next_run`` as UTC."""
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    when = datetime.fromisoformat(text)
    return as_utc(when)


def due_jobs(jobs: list[Job], now: datetime) -> list[Job]:
    """Jobs that should fire once at ``now``. Paused jobs never qualify."""
    now_utc = as_utc(now)
    due: list[Job] = []
    for job in jobs:
        if job.paused:
            continue
        if job.next_run is None:
            continue
        if parse_next_run(job.next_run) <= now_utc:
            due.append(job)
    return due


def next_run_after(cadence: str, now: datetime) -> datetime:
    """The next fire strictly after ``now``. One step, not N missed slots."""
    now_utc = as_utc(now)
    interval = _INTERVAL.fullmatch(cadence)
    if interval is not None:
        count = int(interval.group(1))
        unit = _UNITS[interval.group(2).lower()]
        delta = {
            "minute": timedelta(minutes=count),
            "hour": timedelta(hours=count),
            "day": timedelta(days=count),
        }[unit]
        return now_utc + delta
    return _next_cron(cadence, now_utc)


def advance_job(job: Job, now: datetime) -> Job:
    """One-shot jobs are spent. Recurring jobs get a single future ``next_run``.

    A spent one-shot loses its ``next_run`` as well as being paused. Leaving
    the old slot behind means the row is due the instant anyone unpauses it,
    and Pause/Resume is the only handle the rail gives a job — so a user
    glancing at last week's finished job and toggling it would fire the
    instruction again with no warning. Cleared, Resume is inert and the row
    reads "Unscheduled", which is what a job with nothing left to do is.
    """
    if job.cadence is None:
        return job.model_copy(update={"paused": True, "next_run": None})
    nxt = next_run_after(job.cadence, now)
    return job.model_copy(update={"next_run": normalize_next_run(nxt.isoformat())})


def record_run(
    job: Job,
    now: datetime,
    *,
    status: RunStatus,
    summary: str,
    session_id: str | None,
) -> Job:
    """Stamp the outcome of a fire onto the job (TD-3807).

    Kept separate from ``advance_job`` so the two reasons a job changes —
    "it is due again at X" and "here is what happened last time" — stay
    legible, and so a caller can advance without claiming a run happened.
    """
    return job.model_copy(
        update={
            "last_run": normalize_next_run(as_utc(now).isoformat()),
            "last_status": status,
            "last_summary": normalize_summary(summary),
            "last_session_id": session_id,
        }
    )


def arm_cadence_job(job: Job, now: datetime) -> Job:
    """Give a cadence-only job its first ``next_run`` without firing it."""
    if job.cadence is None or job.next_run is not None:
        return job
    nxt = next_run_after(job.cadence, now)
    return job.model_copy(update={"next_run": normalize_next_run(nxt.isoformat())})


def _next_cron(expr: str, after: datetime) -> datetime:
    fields = expr.split()
    if len(fields) != 5:
        raise JobError(f"cadence is not a 5-field cron expression: {expr!r}")
    cursor = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = cursor + _CRON_HORIZON
    while cursor < limit:
        if _cron_matches(fields, cursor):
            return cursor
        cursor += timedelta(minutes=1)
    raise JobError("no next cron fire within a year")


def _cron_matches(fields: list[str], when: datetime) -> bool:
    minute, hour, day, month, dow = fields
    if not _field_matches(minute, when.minute):
        return False
    if not _field_matches(hour, when.hour):
        return False
    if not _field_matches(month, when.month):
        return False
    return _day_matches(day, dow, when)


def _day_matches(day: str, dow: str, when: datetime) -> bool:
    """Vixie-cron day semantics: restrict both fields and they OR.

    When day-of-month and day-of-week are *both* restricted, cron fires on
    a day matching *either* — ``0 0 1 * 1`` is "the 1st, and every Monday",
    not "Mondays that fall on the 1st".  When only one is restricted the
    other is ``*`` and contributes nothing, so a plain AND is the same
    answer.  Getting this wrong silently drops most of a schedule's fires.
    """
    day_restricted = day.strip() != "*"
    dow_restricted = dow.strip() != "*"
    day_hit = _field_matches(day, when.day)
    dow_hit = _dow_matches(dow, when)
    if day_restricted and dow_restricted:
        return day_hit or dow_hit
    return day_hit and dow_hit


def _dow_matches(expr: str, when: datetime) -> bool:
    """Sunday is both 0 and 7 in cron; ``datetime.weekday()`` has Monday 0."""
    cron_dow = (when.weekday() + 1) % 7
    if _field_matches(expr, cron_dow):
        return True
    return cron_dow == 0 and _field_matches(expr, 7)


def _field_matches(expr: str, value: int) -> bool:
    return any(_part_matches(part, value) for part in expr.split(","))


def _part_matches(part: str, value: int) -> bool:
    step = 1
    base = part
    if "/" in part:
        base, step_s = part.split("/", 1)
        step = int(step_s)
    if base == "*":
        return value % step == 0
    if "-" in base:
        low_s, high_s = base.split("-", 1)
        low, high = int(low_s), int(high_s)
        if not (low <= value <= high):
            return False
        return (value - low) % step == 0
    return value == int(base) and (step == 1 or value % step == 0)
