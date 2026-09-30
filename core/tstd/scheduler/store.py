"""Persist scheduled jobs under the user data dir (TD-3803).

Path: ``{data_dir}/scheduler/jobs.json``. Atomic replace. Not the
workspace, not git. This module does not start a session and has no
event loop — jobs stay inert until TD-3804.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..logging import get_logger
from .history import delete_history
from .models import Job, JobDraft, JobValidationError, validate_draft

log = get_logger("tstd.scheduler.store")

_ENVELOPE_VERSION = 1
_JOBS_FILE = "jobs.json"
# Re-entrant: ``save_job`` lists under the same lock it holds, and a
# park settle must read and write as one step. A miss that saved a
# stale copy would put ``parked_session_id`` back after the watcher
# had cleared it. History has its own lock; never take that one while
# this one is held (``delete_job`` takes this one first, then history).
_STORE_LOCK = threading.RLock()


def jobs_path(data_dir: str | Path) -> Path:
    """``{user_data_dir}/scheduler/jobs.json``."""
    return Path(data_dir) / "scheduler" / _JOBS_FILE


def list_jobs(data_dir: str | Path) -> list[Job]:
    """Load jobs. Absent or unreadable is empty; malformed rows are dropped."""
    with _STORE_LOCK:
        return _read_jobs(data_dir)


def get_job(data_dir: str | Path, job_id: str) -> Job | None:
    with _STORE_LOCK:
        for job in _read_jobs(data_dir):
            if job.id == job_id:
                return job
    return None


def save_job(data_dir: str | Path, job: Job | JobDraft) -> Job:
    """Validate (if needed) and persist. Does not run the job."""
    record = job if isinstance(job, Job) else validate_draft(job)
    with _STORE_LOCK:
        return _upsert(data_dir, record)


def transform_job(
    data_dir: str | Path,
    job_id: str,
    mutate: Callable[[Job], Job | None],
) -> Job | None:
    """Read-modify-write one job. None when it is missing or mutate declines.

    The lock is held across the read and the write so two updates of one
    row cannot put back a field the other just cleared. ``mutate`` must
    not touch the history log: this lock is not the history lock.
    """
    with _STORE_LOCK:
        jobs = _read_jobs(data_dir)
        for index, item in enumerate(jobs):
            if item.id != job_id:
                continue
            updated = mutate(item)
            if updated is None:
                return None
            jobs[index] = updated
            _write_jobs(Path(data_dir), jobs)
            return updated
    return None


def delete_job(data_dir: str | Path, job_id: str) -> bool:
    """Remove a job by id, and its run history. False when it was not present."""
    with _STORE_LOCK:
        jobs = _read_jobs(data_dir)
        kept = [item for item in jobs if item.id != job_id]
        if len(kept) == len(jobs):
            return False
        _write_jobs(Path(data_dir), kept)
        # The id came from a row that validated, so it is a single path segment.
        # History lock is taken second. Callers that update a run take the
        # history lock only, then this one, and never the other way around.
        delete_history(data_dir, job_id)
    return True


def _read_jobs(data_dir: str | Path) -> list[Job]:
    path = jobs_path(data_dir)
    if not path.exists():
        return []
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "scheduler store unreadable, starting empty",
            extra={"extra_fields": {"path": str(path), "error": str(exc)}},
        )
        return []
    rows = _rows_from_envelope(raw)
    if rows is None:
        return []
    jobs: list[Job] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        try:
            jobs.append(Job.model_validate(entry))
        except (ValidationError, JobValidationError):
            log.warning(
                "dropping malformed scheduled job",
                extra={"extra_fields": {"entry": entry}},
            )
    return jobs


def _upsert(data_dir: str | Path, record: Job) -> Job:
    jobs = _read_jobs(data_dir)
    for index, item in enumerate(jobs):
        if item.id == record.id:
            jobs[index] = record
            _write_jobs(Path(data_dir), jobs)
            return record
    jobs.append(record)
    _write_jobs(Path(data_dir), jobs)
    return record


def _rows_from_envelope(raw: Any) -> list[Any] | None:
    if isinstance(raw, dict):
        version = raw.get("version", _ENVELOPE_VERSION)
        if version != _ENVELOPE_VERSION:
            log.warning(
                "scheduler store version unsupported, starting empty",
                extra={"extra_fields": {"version": version}},
            )
            return None
        rows = raw.get("jobs")
        return rows if isinstance(rows, list) else None
    if isinstance(raw, list):
        return raw
    return None


def _write_jobs(data_dir: Path, jobs: list[Job]) -> None:
    path = jobs_path(data_dir)
    payload = {
        "version": _ENVELOPE_VERSION,
        "jobs": [job.model_dump() for job in jobs],
    }
    text = json.dumps(payload, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".jobs.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # Owner-only before the rename. POSIX mode bits only: on Windows
        # os.chmod can merely toggle the read-only flag (TD-1406).
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
