"""Pydantic job schema (TD-3803).

A draft is what a worker would fill from natural language. It is not a
job until ``validate_draft`` succeeds. Validation never writes disk.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ..logging import redact_secrets

DeliverTo = Literal["window", "slack", "ntfy"]
RunStatus = Literal["ok", "failed"]

#: A run summary is a receipt, not a transcript. Anything longer is cut so
#: jobs.json cannot grow without bound on a job that fires every minute.
MAX_SUMMARY_CHARS = 2000

_CRON_FIELD = re.compile(
    r"^(?:\*(?:/\d+)?|[0-9]+(?:-[0-9]+)?(?:/\d+)?(?:,[0-9]+(?:-[0-9]+)?(?:/\d+)?)*)$"
)
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


class JobError(Exception):
    """Scheduler store error."""


class JobValidationError(JobError, ValueError):
    """Draft or job failed validation. Nothing was persisted."""


class JobDraft(BaseModel):
    """Editable parse result. All fields optional so the user can fill gaps."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    workspace: str | None = None
    instruction: str | None = None
    cadence: str | None = None
    next_run: str | None = None
    deliver_to: DeliverTo | None = None
    paused: bool = False


class Job(BaseModel):
    """A persisted scheduled job.

    Create still supplies exactly one of ``cadence`` or ``next_run``.
    After a run the runner may keep ``cadence`` and stamp ``next_run``
    (TD-3804) so the next fire is a single slot, not a catch-up burst.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    cadence: str | None = None
    next_run: str | None = None
    deliver_to: DeliverTo
    paused: bool = False

    # ── Last run (TD-3807) ────────────────────────────────────────────
    # Optional so a jobs.json written before this landed still loads.
    # Without them a job is a black box: it fires, and nothing on disk or
    # on screen says whether it ever worked.
    last_run: str | None = None
    last_status: RunStatus | None = None
    last_summary: str | None = None
    last_session_id: str | None = None

    @field_validator("id")
    @classmethod
    def _id_is_a_name(cls, value: str) -> str:
        if Path(value).name != value or not value.strip():
            raise ValueError("id must be a single path segment")
        return value

    @field_validator("workspace")
    @classmethod
    def _absolute_workspace(cls, value: str) -> str:
        return normalize_workspace(value)

    @field_validator("instruction")
    @classmethod
    def _instruction_plain(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("instruction is required")
        reject_secrets(text, "instruction")
        return text

    @field_validator("cadence")
    @classmethod
    def _cadence_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_cadence(value)

    @field_validator("next_run")
    @classmethod
    def _next_run_iso(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_next_run(value)

    @field_validator("last_run")
    @classmethod
    def _last_run_iso(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_next_run(value)

    @field_validator("last_summary")
    @classmethod
    def _last_summary_is_safe(cls, value: str | None) -> str | None:
        return normalize_summary(value)

    @model_validator(mode="after")
    def _one_schedule(self) -> Job:
        """A job needs a schedule, unless it has already spent the one it had.

        ``advance_job`` clears a one-shot's ``next_run`` once it fires, so
        that Resume cannot re-run last week's instruction on the next tick.
        That leaves a row with neither field, which is the honest shape for
        a job with nothing left to do. Create is guarded separately by
        ``validate_draft``, which still demands exactly one of the two — so
        this only widens what may be *loaded*, never what may be made.
        """
        if self.cadence is None and self.next_run is None and self.last_run is None:
            raise ValueError("cadence or next_run is required")
        return self


def reject_secrets(text: str, field: str) -> None:
    """Refuse secret-shaped text so jobs.json never holds a key."""
    if redact_secrets(text) != text:
        raise JobValidationError(f"{field} must not contain secrets")


def normalize_workspace(raw: str) -> str:
    """Absolute filesystem path, no credentials, not a URL."""
    text = raw.strip()
    if not text:
        raise JobValidationError("workspace is required")
    reject_secrets(text, "workspace")
    if "://" in text:
        raise JobValidationError("workspace must be a filesystem path")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise JobValidationError("workspace must be an absolute path")
    return os.path.normpath(str(path))


def normalize_cadence(raw: str) -> str:
    """Canonical cron (5 fields) or ``every N {minute|hour|day}[s]``."""
    text = " ".join(raw.split())
    if not text:
        raise JobValidationError("cadence is empty")
    interval = _INTERVAL.fullmatch(text)
    if interval is not None:
        count = int(interval.group(1))
        stem = _UNITS[interval.group(2).lower()]
        unit = stem if count == 1 else f"{stem}s"
        return f"every {count} {unit}"
    fields = text.split()
    if len(fields) == 5 and all(_CRON_FIELD.fullmatch(part) for part in fields):
        return " ".join(fields)
    raise JobValidationError(
        "cadence must be a 5-field cron expression or 'every N minutes|hours|days'"
    )


def normalize_summary(raw: str | None) -> str | None:
    """Redact and cap a run summary. The turn's own text, so not trusted.

    ``instruction`` *rejects* secret-shaped text because the user typed it
    and can retype it. A summary is model output arriving after the fact —
    refusing it would throw away the run record, so redact instead.

    Module level, not just a validator: ``model_copy`` does not re-validate,
    and the runner stamps the summary that way.
    """
    if raw is None:
        return None
    text = redact_secrets(raw).strip()
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[:MAX_SUMMARY_CHARS] + "…"
    return text or None


def normalize_next_run(raw: str) -> str:
    """Timezone-aware ISO-8601. Naive values are stored as UTC."""
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError as exc:
        raise JobValidationError("next_run must be an ISO-8601 datetime") from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.isoformat()


def validate_draft(draft: JobDraft) -> Job:
    """Turn an editable draft into a Job. Does not persist."""
    missing: list[str] = []
    if not (draft.workspace and draft.workspace.strip()):
        missing.append("workspace")
    if not (draft.instruction and draft.instruction.strip()):
        missing.append("instruction")
    if draft.deliver_to is None:
        missing.append("deliver_to")
    has_cadence = bool(draft.cadence and draft.cadence.strip())
    has_next = bool(draft.next_run and draft.next_run.strip())
    if has_cadence and has_next:
        raise JobValidationError("provide cadence or next_run, not both")
    if not has_cadence and not has_next:
        missing.append("cadence or next_run")
    if missing:
        raise JobValidationError("missing " + ", ".join(missing))
    try:
        return Job(
            id=draft.id or str(uuid.uuid4()),
            workspace=draft.workspace or "",
            instruction=draft.instruction or "",
            cadence=draft.cadence,
            next_run=draft.next_run,
            deliver_to=draft.deliver_to or "window",
            paused=draft.paused,
        )
    except (ValidationError, JobValidationError) as exc:
        if isinstance(exc, JobValidationError):
            raise
        raise JobValidationError(str(exc)) from exc
