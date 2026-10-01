"""Skip a regular slot that falls on a local calendar block (TD-3818).

The file is read from disk. Nothing is fetched. A skip is expected, so
the channel is not notified and the event title is not recorded: the
receipt says ``calendar`` and nothing else about the day. An unreadable
file is not a skip. The job still runs, and the receipt notes that once.

The cache is one read per path per tick, keyed by mtime. ``begin_tick``
forgets which paths this tick already opened. A missing file is not
stored under an mtime, so the next tick looks again.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from pathlib import Path
from typing import Literal

from ..logging import get_logger
from .history import append_run
from .ics import CalendarFile, parse_ics, slot_blocked
from .models import MAX_SUMMARY_CHARS, Job
from .schedule import advance_job, parse_next_run, record_run
from .store import save_job

log = get_logger("tstd.scheduler.calendar")

SKIP_REASON = "calendar"
UNREADABLE_NOTE = "calendar unreadable"
# A calendar someone exported by mistake can be enormous. Past this the
# file is unreadable and the job runs, rather than the tick reading it.
_MAX_BYTES = 2 * 1024 * 1024

_lock = threading.Lock()
_mtime: dict[str, tuple[int, CalendarFile]] = {}
_tick: dict[str, CalendarFile] = {}

Verdict = Literal["skip", "run", "unreadable"]


def begin_tick() -> None:
    """Allow each path to be read once more. The mtime cache stays."""
    with _lock:
        _tick.clear()


def load_calendar(path: str) -> CalendarFile:
    """The calendar at ``path``, from this tick's first read of it."""
    with _lock:
        seen = _tick.get(path)
        if seen is not None:
            return seen
        parsed = _load_fresh(path)
        _tick[path] = parsed
        return parsed


def _load_fresh(path: str) -> CalendarFile:
    """Read when the mtime changed. Caller holds ``_lock``."""
    try:
        st = Path(path).stat()
    except OSError:
        _mtime.pop(path, None)
        return CalendarFile((), True)
    cached = _mtime.get(path)
    if cached is not None and cached[0] == st.st_mtime_ns:
        return cached[1]
    if st.st_size > _MAX_BYTES:
        parsed = CalendarFile((), True)
        _mtime[path] = (st.st_mtime_ns, parsed)
        return parsed
    try:
        text = _read_text(Path(path))
    except (OSError, UnicodeError):
        parsed = CalendarFile((), True)
        _mtime[path] = (st.st_mtime_ns, parsed)
        return parsed
    parsed = parse_ics(text)
    _mtime[path] = (st.st_mtime_ns, parsed)
    return parsed


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


async def consider_calendar(job: Job) -> Verdict:
    """``skip`` only for a regular slot inside a matching event.

    A retry (``attempt`` > 0) is not a regular slot, so a blocked day
    still runs. An unreadable file is reported either way: the receipt
    of the try that consulted it says so.
    """
    path = job.skip_calendar
    slot_raw = job.next_run
    if path is None or slot_raw is None:
        return "run"
    parsed = await asyncio.to_thread(load_calendar, path)
    if parsed.unreadable:
        return "unreadable"
    if job.attempt != 0:
        return "run"
    slot = parse_next_run(slot_raw)
    try:
        blocked = slot_blocked(parsed, slot, job.timezone, job.skip_match)
    except (KeyError, ValueError, OSError, OverflowError, TypeError, IndexError):
        # The title stays in the file. The log names the job only.
        log.info(
            "scheduled calendar unreadable",
            extra={"extra_fields": {"job_id": job.id}},
        )
        return "unreadable"
    if blocked:
        return "skip"
    return "run"


async def skip_for_calendar(data_dir: Path, job: Job, now: datetime) -> None:
    """Advance, record ``skipped``, and deliver nothing.

    Save happens before the caller returns, same as a real fire: an
    overdue ``next_run`` left on disk would skip again on the next tick.
    The summary is the word ``calendar``. The event title is not a receipt.
    """
    slot_raw = job.next_run
    if slot_raw is None:
        return
    stamped = record_run(
        advance_job(job, now).model_copy(update={"attempt": 0, "resume_at": None}),
        now,
        status="skipped",
        summary=SKIP_REASON,
        session_id=None,
    )
    await asyncio.to_thread(save_job, data_dir, stamped)
    log.info(
        "scheduled slot skipped, calendar",
        extra={"extra_fields": {"job_id": job.id}},
    )
    await asyncio.to_thread(
        append_run,
        data_dir,
        job.id,
        started_at=now,
        scheduled_for=slot_raw,
        trigger="schedule",
        status="skipped",
        summary=SKIP_REASON,
        session_id=None,
    )


def note_unreadable(summary: str) -> str:
    """Append the unreadable note once, inside the summary cap.

    ``normalize_summary`` cuts anything longer and would drop a note
    glued on past the cap, so the room is reserved here.
    """
    text = summary.strip()
    if UNREADABLE_NOTE.casefold() in text.casefold():
        return text
    if not text:
        return UNREADABLE_NOTE
    suffix = f" \u2014 {UNREADABLE_NOTE}"
    room = MAX_SUMMARY_CHARS - len(suffix)
    if len(text) > room:
        text = text[:room].rstrip()
    if not text:
        return UNREADABLE_NOTE
    return f"{text}{suffix}"
