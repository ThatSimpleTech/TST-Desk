"""How long one scheduled turn may run (TD-3819).

A research job makes many tool calls. Stopping the waiter at 120 seconds
left that turn running and recorded a timeout, which the retry table
treats as transient. The receipt names a max run time instead, and the
session is cancelled so the slot is actually free.
"""

from __future__ import annotations

from .grace import GraceError, parse_grace

# The config default lives on ``SchedulerConfig``. These bounds are the
# per-job override: a phrase and a stored second count are one limit, and
# a job cannot ask for longer than an hour.
MIN_JOB_MAX_RUN_SECONDS = 60
MAX_JOB_MAX_RUN_SECONDS = 60 * 60

# The classifier matches these. The sentence is what a receipt shows when
# the code is missing; the code is what the runner records.
MAX_RUN_ERROR_CODE = "max_run"
MAX_RUN_MARK = "max run time"


class MaxRunError(ValueError):
    """The limit could not be stored. Nothing was written."""


def parse_max_run(value: object) -> int | None:
    """Seconds, or None when the field is blank.

    Same phrases as grace and ``retry_delay``. Blank means the job uses
    ``scheduler.max_run_seconds``. A stored count must be 1-60 minutes.
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise MaxRunError("must be from 1 to 60 minutes")
    try:
        seconds = parse_grace(value)
    except GraceError as exc:
        raise MaxRunError(str(exc)) from None
    if seconds is None:
        return None
    if seconds < MIN_JOB_MAX_RUN_SECONDS or seconds > MAX_JOB_MAX_RUN_SECONDS:
        raise MaxRunError("must be from 1 to 60 minutes")
    return seconds


def run_limit_seconds(job_max: int | None, config_seconds: float) -> float:
    """The wait for this fire. A job's own limit wins over the config."""
    if job_max is not None:
        return float(job_max)
    return float(config_seconds)


def stop_summary(seconds: float) -> str:
    """The receipt. Whole minutes, rounded up, so it never claims less.

    A limit under a minute still says one minute: the sentence has no
    seconds form, and the mark is what the retry table refuses.
    """
    whole = int(seconds)
    if whole < 0:
        whole = 0
    minutes = max(1, (whole + 59) // 60)
    unit = "minute" if minutes == 1 else "minutes"
    return f"stopped after {minutes} {unit} ({MAX_RUN_MARK})"
