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
from typing import Literal, cast
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ..logging import redact_secrets
from .grace import GraceError, parse_grace
from .phrases import expand_alias, phrase_to_cron
from .pin import normalize_engine, normalize_preset
from .retry import (
    DEFAULT_RETRY_DELAY_SECONDS,
    RetryError,
    parse_retries,
    parse_retry_delay,
)

DeliverTo = Literal["window", "slack", "ntfy"]
RunStatus = Literal["ok", "failed", "missed", "waiting"]

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
# Inclusive bounds per cron field. Day-of-week allows 7 because Sunday is
# both 0 and 7. Checked at save time so a typo is refused with the form still
# open, instead of failing inside the runner's next-fire search.
_CRON_BOUNDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 7),
)
_FIELD_LABELS = {
    "workspace": "Workspace",
    "instruction": "Instruction",
    "cadence": "Cadence",
    "next_run": "Next run",
    "deliver_to": "Deliver to",
    "timezone": "Time zone",
    "preset": "Preset",
    "engine": "Engine",
    "grace": "If late",
    "retries": "Retries",
    "retry_delay": "Retry delay",
}
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
    timezone: str | None = None
    # Catalog name and engine kind (TD-3812). Blank becomes None in ``Job``.
    preset: str | None = None
    engine: str | None = None
    # Phrase ("2 hours") or seconds. Blank is no grace. ``Job`` stores seconds.
    grace: str | int | None = None
    # Extra tries after a transient scheduled failure (TD-3814). Blank is
    # none. ``retry_delay`` is a phrase or seconds; blank becomes the
    # 10-minute default once retries is at least 1.
    retries: int | None = None
    retry_delay: str | int | None = None


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
    # IANA name the cron cadence is read in. None is UTC, which is what every
    # job saved before this field existed already means.
    timezone: str | None = None
    # Catalog preset name and engine kind for this job's runs (TD-3812).
    # None means "whatever the window is using when the job fires". The
    # catalog is not checked here: a name that later leaves the catalog
    # must still load, so the run can fail with a receipt and pause still
    # works. Slugs, URLs, and keys are not stored.
    preset: str | None = None
    engine: Literal["native", "grok"] | None = None
    # Seconds a slot may be late and still run (TD-3813). None fires a
    # missed slot once, however old it is. The phrase is not stored:
    # "2 hours" and 7200 are one window, and the tick subtracts it.
    grace: int | None = None
    # Extra tries after the first scheduled fire (TD-3814). 0 is the old
    # behaviour: a failure waits for the next regular slot. The delay is
    # seconds. None only while retries is 0; a positive count with no
    # delay becomes 10 minutes in the model validator.
    retries: int = 0
    retry_delay: int | None = None
    # Tries already used for the slot in progress. 0 means the next fire
    # is the regular slot (grace applies). Above 0, ``next_run`` is the
    # retry instant and ``resume_at`` is the regular slot to restore.
    attempt: int = 0
    resume_at: str | None = None

    # ── Last run (TD-3807) ────────────────────────────────────────────
    # Optional so a jobs.json written before this landed still loads.
    # Without them a job is a black box: it fires, and nothing on disk or
    # on screen says whether it ever worked.
    last_run: str | None = None
    last_status: RunStatus | None = None
    last_summary: str | None = None
    last_session_id: str | None = None
    # The session parked on an approval card, and the history line that
    # records it (TD-3815). Disk only: a restart has to find the run
    # after the process that was waiting is gone. The pane uses
    # ``last_status`` and ``last_session_id``; these two are not on the wire.
    parked_session_id: str | None = None
    parked_started_at: str | None = None

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

    @field_validator("timezone")
    @classmethod
    def _timezone_known(cls, value: str | None) -> str | None:
        return normalize_timezone(value)

    @field_validator("preset", mode="before")
    @classmethod
    def _preset_name(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be a catalog name")
        return normalize_preset(value)

    @field_validator("engine", mode="before")
    @classmethod
    def _engine_kind(cls, value: object) -> Literal["native", "grok"] | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be native or grok")
        return normalize_engine(value)

    @field_validator("grace", mode="before")
    @classmethod
    def _grace_seconds(cls, value: object) -> int | None:
        # The annotation is the stored seconds. The phrase is accepted
        # here so a hand-edited jobs.json and a save both land as one int.
        try:
            return parse_grace(value)
        except GraceError as exc:
            raise ValueError(str(exc)) from None

    @field_validator("retries", mode="before")
    @classmethod
    def _retries_count(cls, value: object) -> int:
        try:
            return parse_retries(value)
        except RetryError as exc:
            raise ValueError(str(exc)) from None

    @field_validator("retry_delay", mode="before")
    @classmethod
    def _retry_delay_seconds(cls, value: object) -> int | None:
        try:
            return parse_retry_delay(value)
        except RetryError as exc:
            raise ValueError(str(exc)) from None

    @field_validator("attempt", mode="before")
    @classmethod
    def _attempt_count(cls, value: object) -> int:
        # Null is the same as absent: a jobs.json from before retries
        # has no counter, and a hand edit that writes null must still load.
        if value is None:
            return 0
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("must be a whole number")
        return value

    @field_validator("resume_at")
    @classmethod
    def _resume_at_iso(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return normalize_next_run(value)

    @field_validator("parked_session_id")
    @classmethod
    def _parked_session(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("parked_started_at")
    @classmethod
    def _parked_started_iso(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return normalize_next_run(value)

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
        # The default delay depends on the count, so it cannot live on the
        # delay field alone. A count of 0 stores no delay: the number would
        # not be used, and a later edit that turns retries on names its own.
        delay = self.retry_delay
        if self.retries > 0 and delay is None:
            delay = DEFAULT_RETRY_DELAY_SECONDS
        elif self.retries == 0:
            delay = None
        if delay != self.retry_delay:
            self.retry_delay = delay
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
        raise JobValidationError(
            f"must be a full folder path such as ~/Documents/project (got {raw.strip()!r})"
        )
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
        _check_cron_ranges(fields, text)
        return " ".join(fields)
    cron = expand_alias(text) or phrase_to_cron(text)
    if cron is not None:
        return cron
    raise JobValidationError(
        f"cadence not understood: {text!r}. "
        "Try 'weekdays at 7:45', 'every 2 hours', or cron like '45 7 * * 1-5'"
    )


def _check_cron_ranges(fields: list[str], text: str) -> None:
    for (name, low, high), field in zip(_CRON_BOUNDS, fields, strict=True):
        for part in field.split(","):
            base, _, step = part.partition("/")
            numbers = [] if base == "*" else [int(n) for n in base.split("-")]
            in_range = all(low <= n <= high for n in numbers)
            if not in_range or numbers != sorted(numbers) or step == "0":
                raise JobValidationError(
                    f"cadence has an impossible {name} in {text!r} "
                    f"(allowed {low}-{high}); cron is 'minute hour day month weekday'"
                )


def normalize_timezone(raw: str | None) -> str | None:
    """A known IANA zone name, or None for UTC. Blank means unset."""
    if raw is None or not raw.strip():
        return None
    name = raw.strip()
    try:
        ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        # Unknown names are ZoneInfoNotFoundError (a KeyError), malformed
        # keys like "../etc" are ValueError, a directory name like "America"
        # is an OSError. To the user they are all the same mistake.
        raise JobValidationError(f"timezone '{name}' is not a known IANA time zone") from None
    return name


def describe_validation_error(exc: ValidationError) -> str:
    """One human line for a pydantic failure: ``Workspace: …; Cadence: …``.

    The default rendering is a multi-line dump with a docs link, which the
    window shows verbatim. Each part is cut down to the message the
    validator wrote; a model-level error has no field and is the bare message.
    """
    parts: list[str] = []
    for error in exc.errors():
        message = error["msg"].removeprefix("Value error, ")
        field = str(error["loc"][0]) if error["loc"] else ""
        label = _FIELD_LABELS.get(field, field.replace("_", " ").capitalize())
        parts.append(f"{label}: {_drop_field_name(message, field)}" if label else message)
    return "; ".join(parts)


def _drop_field_name(message: str, field: str) -> str:
    """Validators name their own field ("cadence not understood"); after the
    label that reads twice, so the repeat goes."""
    for name in (field, field.replace("_", " ")):
        if name and message.lower().startswith(name + " "):
            return message[len(name) + 1 :]
    return message


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
            timezone=draft.timezone,
            preset=draft.preset,
            # The field is the stored kind. The validator still accepts "",
            # "Native", and a bad string, and turns the last into the error.
            engine=cast(Literal["native", "grok"] | None, draft.engine),
            # Same as engine: the annotation is seconds, the validator
            # accepts the phrase and a blank.
            grace=cast(int | None, draft.grace),
            retries=0 if draft.retries is None else draft.retries,
            retry_delay=cast(int | None, draft.retry_delay),
        )
    except ValidationError as exc:
        raise JobValidationError(describe_validation_error(exc)) from exc
