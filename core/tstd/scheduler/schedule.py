"""When a job is due and how the cadence advances (TD-3804).

A missed ``next_run`` is one fire, then the next slot is computed from
*now* — never a catch-up loop over every skipped interval.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from .models import Job, JobError, normalize_next_run

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
    """One-shot jobs pause. Recurring jobs get a single future ``next_run``."""
    if job.cadence is None:
        return job.model_copy(update={"paused": True})
    nxt = next_run_after(job.cadence, now)
    return job.model_copy(update={"next_run": normalize_next_run(nxt.isoformat())})


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
    cron_dow = (when.weekday() + 1) % 7
    return (
        _field_matches(minute, when.minute)
        and _field_matches(hour, when.hour)
        and _field_matches(day, when.day)
        and _field_matches(month, when.month)
        and (_field_matches(dow, cron_dow) or _field_matches(dow, 7 if cron_dow == 0 else cron_dow))
    )


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
