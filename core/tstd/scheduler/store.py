"""Persist scheduled jobs under the user data dir (TD-3803).

Path: ``{data_dir}/scheduler/jobs.json``. Atomic replace. Not the
workspace, not git. This module does not start a session and has no
event loop — jobs stay inert until TD-3804.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..logging import get_logger
from .models import Job, JobDraft, JobValidationError, validate_draft

log = get_logger("tstd.scheduler.store")

_ENVELOPE_VERSION = 1
_JOBS_FILE = "jobs.json"


def jobs_path(data_dir: str | Path) -> Path:
    """``{user_data_dir}/scheduler/jobs.json``."""
    return Path(data_dir) / "scheduler" / _JOBS_FILE


def list_jobs(data_dir: str | Path) -> list[Job]:
    """Load jobs. Absent or unreadable is empty; malformed rows are dropped."""
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


def get_job(data_dir: str | Path, job_id: str) -> Job | None:
    for job in list_jobs(data_dir):
        if job.id == job_id:
            return job
    return None


def save_job(data_dir: str | Path, job: Job | JobDraft) -> Job:
    """Validate (if needed) and persist. Does not run the job."""
    record = job if isinstance(job, Job) else validate_draft(job)
    jobs = list_jobs(data_dir)
    for index, item in enumerate(jobs):
        if item.id == record.id:
            jobs[index] = record
            _write_jobs(Path(data_dir), jobs)
            return record
    jobs.append(record)
    _write_jobs(Path(data_dir), jobs)
    return record


def delete_job(data_dir: str | Path, job_id: str) -> bool:
    """Remove a job by id. Returns False when it was not present."""
    jobs = list_jobs(data_dir)
    kept = [item for item in jobs if item.id != job_id]
    if len(kept) == len(jobs):
        return False
    _write_jobs(Path(data_dir), kept)
    return True


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
