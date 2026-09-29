"""One JSONL file of runs per job (TD-3811).

``jobs.json`` keeps a single receipt (``last_*``). Each fire used to
overwrite it, so the pane could not show an earlier run or the session
it happened in. The log lives under the user data dir —
``{data_dir}/scheduler/history/<job_id>.jsonl`` — and never in the
workspace, which is git-tracked and moves independently of the daemon.

The module is synchronous, like the job store. Callers write it with
``asyncio.to_thread``. A line that does not parse is skipped: one bad
record must not hide the rest or fail the request that asked for them.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from ..logging import get_logger
from .models import JobValidationError, RunStatus, normalize_next_run, normalize_summary
from .schedule import as_utc

log = get_logger("tstd.scheduler.history")

# A job that fires every minute would grow without a bound. Fifty is
# enough to see what changed; the row's last_* is the newest either way.
HISTORY_CAP = 50

RunTrigger = Literal["schedule", "manual"]

_HISTORY_DIR = "history"


class JobRun(BaseModel):
    """One fire, scheduled or Run now."""

    model_config = ConfigDict(extra="ignore")

    started_at: str
    scheduled_for: str | None = None
    trigger: RunTrigger
    status: RunStatus
    summary: str | None = None
    session_id: str | None = None

    @field_validator("started_at")
    @classmethod
    def _started_at_iso(cls, value: str) -> str:
        return normalize_next_run(value)

    @field_validator("scheduled_for")
    @classmethod
    def _scheduled_for_iso(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_next_run(value)

    @field_validator("summary")
    @classmethod
    def _summary_is_safe(cls, value: str | None) -> str | None:
        # model_copy does not re-validate, and neither does a hand-edited
        # line. Constructing the model is what redacts before the write
        # and again on the way out.
        return normalize_summary(value)

    @field_validator("session_id")
    @classmethod
    def _session_or_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


def history_path(data_dir: str | Path, job_id: str) -> Path:
    """``{data_dir}/scheduler/history/<job_id>.jsonl``.

    The id is a single path segment, the same rule as ``Job.id``. A name
    that climbs out of the history directory is refused before any open.
    """
    if Path(job_id).name != job_id or not job_id.strip() or job_id in {".", ".."}:
        raise JobValidationError("id must be a single path segment")
    root = Path(data_dir) / "scheduler" / _HISTORY_DIR
    path = root / f"{job_id}.jsonl"
    if path.parent != root:
        raise JobValidationError("id must be a single path segment")
    return path


def list_runs(data_dir: str | Path, job_id: str) -> list[JobRun]:
    """Runs for one job, newest first. A missing file is an empty log."""
    return list(reversed(_read_runs(history_path(data_dir, job_id))))


def append_run(
    data_dir: str | Path,
    job_id: str,
    *,
    started_at: datetime | str,
    scheduled_for: str | None,
    trigger: RunTrigger,
    status: RunStatus,
    summary: str | None,
    session_id: str | None,
) -> JobRun:
    """Append one run and drop the oldest past the cap.

    The file is rewritten, not seek-appended: a crash mid-append would
    leave a torn line, and the cap has to drop from the front anyway.
    Corrupt lines already in the file are left out of the rewrite.
    """
    path = history_path(data_dir, job_id)
    stamp = started_at if isinstance(started_at, str) else as_utc(started_at).isoformat()
    record = JobRun(
        started_at=stamp,
        scheduled_for=scheduled_for,
        trigger=trigger,
        status=status,
        summary=summary,
        session_id=session_id,
    )
    runs = _read_runs(path)
    runs.append(record)
    if len(runs) > HISTORY_CAP:
        runs = runs[-HISTORY_CAP:]
    _write_runs(path, runs)
    return record


def delete_history(data_dir: str | Path, job_id: str) -> bool:
    """Remove the job's log. False when there was nothing to remove."""
    path = history_path(data_dir, job_id)
    if not path.is_file():
        return False
    path.unlink()
    return True


def _read_runs(path: Path) -> list[JobRun]:
    """Oldest first. An unreadable file or a bad line is skipped, not raised."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        log.warning(
            "scheduler history unreadable, treating as empty",
            extra={"extra_fields": {"path": str(path), "error": str(exc)}},
        )
        return []
    runs: list[JobRun] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            runs.append(JobRun.model_validate(raw))
        except (json.JSONDecodeError, ValidationError, JobValidationError):
            # The line can hold a secret that failed to parse. Log that it
            # was dropped, not what it said.
            log.warning(
                "skipping corrupt scheduler history line",
                extra={"extra_fields": {"path": str(path)}},
            )
    return runs


def _write_runs(path: Path, runs: list[JobRun]) -> None:
    text = "".join(run.model_dump_json() + "\n" for run in runs)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".history.", suffix=".tmp")
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
