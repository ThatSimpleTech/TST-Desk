"""Validate a job template the same way a job is validated (TD-3816).

A template may omit the instruction and the workspace. A job may not.
The cadence phrase is checked and then stored as written. A next-run
rule is stored as the rule, not as an instant.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .grace import GraceError, parse_grace
from .limit import MaxRunError, parse_max_run
from .models import (
    DeliverTo,
    JobError,
    JobValidationError,
    normalize_cadence,
    normalize_next_run,
    normalize_workspace,
    reject_secrets,
)
from .pin import EngineKind, normalize_engine, normalize_preset, require_known_preset
from .retry import (
    DEFAULT_RETRY_DELAY_SECONDS,
    RetryError,
    parse_retries,
    parse_retry_delay,
)
from .workspace import resolve_workspace_name

_NAME_MAX = 80
_TOMORROW = re.compile(
    r"^tomorrow at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?$",
    re.IGNORECASE,
)
_DELIVER = ("window", "slack", "ntfy")
_RULE_HINT = "next_run must be an ISO-8601 datetime or 'tomorrow at 9:00'"


class TemplateError(JobError):
    """A template verb failed. ``code`` is the protocol error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class TemplateDraft:
    """What ``save_job_template`` sends. Not yet a stored row."""

    name: str
    instruction: str = ""
    cadence: str | None = None
    next_run: str | None = None
    deliver_to: str | None = None
    grace: str | int | None = None
    retries: str | int | None = None
    retry_delay: str | int | None = None
    max_run: str | int | None = None
    preset: str | None = None
    engine: str | None = None
    workspace: str | None = None


@dataclass(frozen=True)
class TemplateView:
    """One row on ``job_templates``. Built-ins are not in the file."""

    id: str
    name: str
    builtin: bool
    instruction: str
    cadence: str | None
    next_run: str | None
    deliver_to: DeliverTo
    grace: int | None
    retries: int
    retry_delay: int | None
    max_run: int | None
    preset: str | None
    engine: EngineKind | None
    workspace: str | None


def name_limit() -> int:
    """Cap for a template name and for an id shown in an error."""
    return _NAME_MAX


def normalize_rule(raw: str) -> str:
    """ISO-8601, or ``tomorrow at H:MM`` in 24-hour local form."""
    text = " ".join(raw.split())
    reject_secrets(text, "next_run")
    try:
        return normalize_next_run(text)
    except JobValidationError:
        return _canonical_tomorrow(text)


def _canonical_tomorrow(text: str) -> str:
    match = _TOMORROW.fullmatch(text)
    if match is None:
        raise JobValidationError(_RULE_HINT)
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    meridiem = (match.group(3) or "").lower()
    if minute > 59 or (meridiem and (hour < 1 or hour > 12)) or (not meridiem and hour > 23):
        raise JobValidationError(_RULE_HINT)
    if meridiem == "am":
        hour = 0 if hour == 12 else hour
    elif meridiem == "pm":
        hour = hour if hour == 12 else hour + 12
    return f"tomorrow at {hour}:{minute:02d}"


def valid_id(value: str) -> bool:
    """One path segment, and not ``.`` or ``..``."""
    if value in {"", ".", ".."}:
        return False
    if "/" in value or "\\" in value or "\x00" in value:
        return False
    return Path(value).name == value


def validate_template(
    draft: TemplateDraft,
    *,
    template_id: str,
    builtin: bool,
    catalog: Mapping[str, object] | None,
    known_workspaces: Iterable[str],
) -> TemplateView:
    """Turn a draft into a row, or raise ``JobValidationError``."""
    name = " ".join(draft.name.split())
    if name == "":
        raise JobValidationError("name is required")
    if len(name) > _NAME_MAX:
        raise JobValidationError("name must be 80 characters or fewer")
    reject_secrets(name, "name")
    instruction = draft.instruction.strip()
    if instruction:
        reject_secrets(instruction, "instruction")
    cadence_text = " ".join(draft.cadence.split()) if draft.cadence else ""
    next_text = " ".join(draft.next_run.split()) if draft.next_run else ""
    if cadence_text and next_text:
        raise JobValidationError("provide cadence or next_run, not both")
    if not cadence_text and not next_text:
        raise JobValidationError("missing cadence or next_run")
    cadence: str | None = None
    next_run: str | None = None
    if cadence_text:
        reject_secrets(cadence_text, "cadence")
        normalize_cadence(cadence_text)
        cadence = cadence_text
    else:
        next_run = normalize_rule(next_text)
    deliver = draft.deliver_to or "window"
    if deliver not in _DELIVER:
        raise JobValidationError("deliver_to must be window, slack, or ntfy")
    try:
        grace = parse_grace(draft.grace)
    except GraceError as exc:
        raise JobValidationError(f"If late: {exc}") from None
    try:
        retries = parse_retries(draft.retries)
    except RetryError as exc:
        raise JobValidationError(f"Retries: {exc}") from None
    try:
        delay = parse_retry_delay(draft.retry_delay)
    except RetryError as exc:
        raise JobValidationError(f"Retry delay: {exc}") from None
    try:
        max_run = parse_max_run(draft.max_run)
    except MaxRunError as exc:
        raise JobValidationError(f"Max run: {exc}") from None
    if retries > 0 and delay is None:
        delay = DEFAULT_RETRY_DELAY_SECONDS
    elif retries == 0:
        delay = None
    try:
        preset = normalize_preset(draft.preset)
    except ValueError as exc:
        raise JobValidationError(f"Preset: {exc}") from None
    # Load does not pass a catalog. A preset removed later must still list,
    # or the user could not see the template to replace it.
    if catalog is not None:
        require_known_preset(preset, catalog)
    try:
        engine = normalize_engine(draft.engine)
    except ValueError as exc:
        raise JobValidationError(f"Engine: {exc}") from None
    workspace = _workspace(draft.workspace, known_workspaces)
    if not valid_id(template_id):
        raise JobValidationError("id must be a single path segment")
    return TemplateView(
        id=template_id,
        name=name,
        builtin=builtin,
        instruction=instruction,
        cadence=cadence,
        next_run=next_run,
        deliver_to=cast(DeliverTo, deliver),
        grace=grace,
        retries=retries,
        retry_delay=delay,
        max_run=max_run,
        preset=preset,
        engine=engine,
        workspace=workspace,
    )


def _workspace(raw: str | None, known: Iterable[str]) -> str | None:
    """A folder name the daemon already knows, or an absolute path.

    The directory is not required to exist. That check belongs to job
    create: a template can name a folder the user has not made yet.
    """
    if raw is None or not raw.strip():
        return None
    return normalize_workspace(resolve_workspace_name(raw, known))
