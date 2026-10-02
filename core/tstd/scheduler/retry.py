"""Retry a scheduled run that failed for a transient reason (TD-3814).

The provider client already retries a single call. This is the later
try: EZER was down at 7:45, so the job goes again after a few minutes
instead of waiting until tomorrow. Only a scheduled fire does this.
Run now is one shot the user asked for, and a miss for lateness is not
a failure.

``retries`` is how many extra tries follow the first. ``attempt`` on the
job is how many of this slot have already run. The regular slot is
remembered on ``resume_at`` because an interval cadence is ``now`` plus
the interval: advancing at the retry instant would walk 7:45 to 7:55
forever. Grace does not apply while ``attempt`` is set; that instant is
the retry, not a new morning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from .grace import GraceError, parse_grace
from .limit import MAX_RUN_ERROR_CODE, MAX_RUN_MARK

if TYPE_CHECKING:
    from .models import Job

DEFAULT_RETRY_DELAY_SECONDS = 10 * 60
MAX_RETRIES = 3

# One table is the whole policy. A reason that matches nothing is not
# transient: an unknown failure waits for the next regular slot rather
# than hammering it. ``http`` matches a status (``429``, ``http_503``).
# Phrases cover a run that never reached a provider, where the only
# signal is the receipt sentence.
TRANSIENT_RULES: tuple[tuple[Literal["exact", "prefix", "contains", "http"], str, bool], ...] = (
    ("exact", "connection_error", True),
    ("exact", "timeout", True),
    ("exact", "timed_out", True),
    ("exact", "rate_limited", True),
    ("exact", "rate_limit", True),
    ("exact", "server_error", True),
    ("exact", "bad_gateway", True),
    ("exact", "service_unavailable", True),
    ("exact", "gateway_timeout", True),
    ("exact", "context_overflow", False),
    ("exact", "context_length_exceeded", False),
    ("exact", "auth_failed", False),
    ("exact", "api_key_rejected", False),
    ("exact", "missing_api_key", False),
    ("exact", "workspace_missing", False),
    ("exact", "preset_missing", False),
    ("exact", "engine_unavailable", False),
    ("exact", "grok engine is unavailable", False),
    ("http", "429", True),
    ("http", "5xx", True),
    ("prefix", "workspace is not a directory", False),
    ("prefix", "preset '", False),
    ("contains", "no longer exists", False),
    # Before "timed out". A job that simply ran out of its own limit
    # must not be tried again; the slot would burn the same way.
    ("exact", MAX_RUN_ERROR_CODE, False),
    ("contains", MAX_RUN_MARK, False),
    ("contains", "timed out", True),
    ("contains", "timeout", True),
    ("contains", "connection error", True),
    ("contains", "connection refused", True),
    ("contains", "connection reset", True),
    ("contains", "connecterror", True),
    ("contains", "rate limit", True),
)


class RetryError(ValueError):
    """Retries or the delay could not be stored. Nothing was written."""


def parse_retries(value: object) -> int:
    """A count from 0 to 3. Blank is no retries. ``True`` is not 1."""
    if value is None:
        return 0
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return 0
        if not text.isdigit():
            raise RetryError("must be a whole number from 0 to 3")
        value = int(text)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RetryError("must be a whole number from 0 to 3")
    if value < 0 or value > MAX_RETRIES:
        raise RetryError("must be a whole number from 0 to 3")
    return value


def parse_retry_delay(value: object) -> int | None:
    """Seconds between tries, or None when the field is blank.

    The 10-minute default depends on ``retries``, which this field does
    not know. Callers apply it after both fields are parsed. The phrases
    are the same ones grace accepts, so "10 minutes" and 600 are one gap.
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return parse_grace(value)
    except GraceError as exc:
        raise RetryError(str(exc)) from None


def is_transient_failure(reason: str | None) -> bool:
    """True when this failed scheduled run should be tried again later.

    ``reason`` is the turn's error code, or the summary when the run
    never got a code (a missing folder, a preset that left the catalog).
    """
    if reason is None:
        return False
    text = " ".join(reason.split()).casefold()
    if text == "":
        return False
    for kind, needle, transient in TRANSIENT_RULES:
        if kind == "exact" and text == needle:
            return transient
        if kind == "prefix" and text.startswith(needle):
            return transient
        if kind == "contains" and needle in text:
            return transient
        if kind == "http" and _http_matches(text, needle):
            return transient
    return False


def failure_reason(*, ok: bool, error_code: str | None, summary: str) -> str | None:
    """The classifier's input. A success has nothing to classify.

    The code wins over the prose: ``context_overflow`` stays non-transient
    even when the sentence mentions a timeout.
    """
    if ok:
        return None
    code = (error_code or "").strip()
    return code or summary


def receipt_text(summary: str, attempt: int | None, attempts: int | None) -> str:
    """The row's receipt. History keeps the raw summary and the numbers."""
    if attempt is None or attempts is None:
        return summary
    return f"attempt {attempt} of {attempts}: {summary}"


@dataclass(frozen=True)
class ScheduledSettle:
    """What one scheduled attempt does to the job. ``last_*`` is separate."""

    updates: dict[str, Any]
    receipt: str
    deliver: bool
    attempt: int | None
    attempts: int | None


def settle_scheduled(
    job: Job,
    now: datetime,
    *,
    ok: bool,
    reason: str | None,
    summary: str,
) -> ScheduledSettle:
    """Advance, retry, or resume the regular slot. Does not write disk."""
    # Local import: ``models`` imports the parsers above, and the schedule
    # module imports ``models``. A top-level import here is a cycle.
    from .models import normalize_next_run
    from .schedule import advance_job, as_utc

    n = job.attempt + 1
    # ``retries`` is the extra tries. The first fire is attempt 1 of
    # retries + 1. A job that does not retry leaves the numbers off the
    # history line so an ordinary receipt stays the sentence it always was.
    show = job.retries > 0
    attempt: int | None = n if show else None
    attempts: int | None = job.retries + 1 if show else None
    receipt = receipt_text(summary, attempt, attempts)
    if ok or not _will_retry(job.retries, n, reason):
        return ScheduledSettle(
            _finished(job, now),
            receipt,
            True,
            attempt,
            attempts,
        )
    # The first failure is the one that knows the regular slot. Later
    # failures keep it: recomputing from the retry clock would drift.
    resume = job.resume_at if job.attempt > 0 else advance_job(job, now).next_run
    delay = job.retry_delay if job.retry_delay is not None else DEFAULT_RETRY_DELAY_SECONDS
    retry_at = as_utc(now) + timedelta(seconds=delay)
    return ScheduledSettle(
        {
            # Leave paused alone. A one-shot is not spent until the slot
            # is finished; pausing it here would swallow the retry.
            "next_run": normalize_next_run(retry_at.isoformat()),
            "attempt": n,
            "resume_at": resume,
        },
        receipt,
        False,
        attempt,
        attempts,
    )


def _will_retry(retries: int, n: int, reason: str | None) -> bool:
    """``n`` is the attempt that just failed. Another try exists while it
    is still within the extra-try budget and the reason is transient."""
    if retries <= 0 or n > retries:
        return False
    return is_transient_failure(reason)


def finish_slot(job: Job, now: datetime) -> dict[str, Any]:
    """Schedule update for a fire that is done with this slot.

    Parking on approval uses the same advance as a success: the slot
    is over, and a retry must not open another session onto the card.
    """
    return _finished(job, now)


def _finished(job: Job, now: datetime) -> dict[str, Any]:
    """Success or the last failure. Counters drop; the regular slot returns.

    A retry of a one-shot spends it the same way the first fire would
    have. A recurring job resumes the slot captured at the first failure,
    not a slot computed from the retry's clock.
    """
    from .schedule import advance_job

    if job.attempt > 0:
        if job.cadence is None:
            return {"paused": True, "next_run": None, "attempt": 0, "resume_at": None}
        nxt = job.resume_at if job.resume_at is not None else advance_job(job, now).next_run
        return {"next_run": nxt, "attempt": 0, "resume_at": None}
    advanced = advance_job(job, now)
    return {
        "paused": advanced.paused,
        "next_run": advanced.next_run,
        "attempt": 0,
        "resume_at": None,
    }


def _http_matches(text: str, needle: str) -> bool:
    raw = text[5:] if text.startswith("http_") else text
    if not raw.isdigit():
        return False
    status = int(raw)
    if needle == "429":
        return status == 429
    if needle == "5xx":
        return 500 <= status <= 599
    return False
