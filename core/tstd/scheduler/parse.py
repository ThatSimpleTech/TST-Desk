"""Deterministic job-request parse (TD-3803).

This is the schema a worker would fill. It does not call a model and it
does not write the store — save is a second call after the user edits.
"""

from __future__ import annotations

import json
import re
from typing import Any, get_args

from pydantic import ValidationError

from .models import DeliverTo, JobDraft, JobValidationError

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_KV = re.compile(
    r"^(id|workspace|instruction|cadence|next_run|deliver_to|paused):\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_DELIVER = re.compile(r"\b(?:deliver(?:\s+to)?|via)\s+(window|slack|ntfy)\b", re.IGNORECASE)
_INTERVAL = re.compile(r"\bevery\s+[1-9]\d*\s+(?:minutes?|hours?|days?)\b", re.IGNORECASE)
_CRON_PREFIX = re.compile(r"\bcron:\s*([^\n]+)", re.IGNORECASE)
_CRON_FIELD = re.compile(
    r"^(?:\*(?:/\d+)?|[0-9]+(?:-[0-9]+)?(?:/\d+)?(?:,[0-9]+(?:-[0-9]+)?(?:/\d+)?)*)$"
)
_NEXT = re.compile(
    r"\b(?:next(?:\s+run)?\s+)?(\d{4}-\d{2}-\d{2}T[0-9:.+-]+Z?)\b",
)
_IN_PATH = re.compile(
    r"\bin\s+((?:~[^\s]*)|(?:/[^\s]+)|(?:[A-Za-z]:\\[^\s]+))",
    re.IGNORECASE,
)
_WORKSPACE_KV = re.compile(r"\bworkspace:\s*(\S+)", re.IGNORECASE)
_PAUSED = re.compile(r"\bpaused\b", re.IGNORECASE)
_LEAD_RUN = re.compile(r"^(?:please\s+)?run\s+", re.IGNORECASE)

_DELIVER_TO: tuple[DeliverTo, ...] = get_args(DeliverTo)


def parse_job_request(text: str) -> JobDraft:
    """Parse natural language or JSON into a JobDraft. Never persists."""
    stripped = text.strip()
    if not stripped:
        return JobDraft()
    fenced = _FENCE.search(stripped)
    candidate = fenced.group(1).strip() if fenced is not None else stripped
    if candidate.startswith("{"):
        return _draft_from_json(candidate)
    if _KV.search(stripped) is not None:
        return _draft_from_key_values(stripped)
    return _draft_from_natural_language(stripped)


def _draft_from_json(text: str) -> JobDraft:
    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JobValidationError(f"job request JSON is invalid: {exc}") from exc
    if not isinstance(raw, dict):
        raise JobValidationError("job request JSON must be an object")
    try:
        return JobDraft.model_validate(raw)
    except ValidationError as exc:
        raise JobValidationError(str(exc)) from exc


def _draft_from_key_values(text: str) -> JobDraft:
    fields: dict[str, str] = {}
    leftover: list[str] = []
    for line in text.splitlines():
        match = _KV.fullmatch(line.strip())
        if match is None:
            if line.strip():
                leftover.append(line.strip())
            continue
        fields[match.group(1).lower()] = match.group(2).strip()
    instruction = fields.get("instruction") or " ".join(leftover) or None
    return JobDraft(
        id=fields.get("id"),
        workspace=fields.get("workspace"),
        instruction=instruction,
        cadence=fields.get("cadence"),
        next_run=fields.get("next_run"),
        deliver_to=_deliver_to(fields.get("deliver_to")),
        paused=_as_bool(fields.get("paused"), default=False),
    )


def _draft_from_natural_language(text: str) -> JobDraft:
    spans: list[tuple[int, int]] = []
    workspace = _take(_WORKSPACE_KV, text, spans) or _take(_IN_PATH, text, spans)
    deliver = _deliver_to(_take(_DELIVER, text, spans))
    cadence = _take(_INTERVAL, text, spans)
    if cadence is None:
        prefixed = _CRON_PREFIX.search(text)
        if prefixed is not None:
            cadence = prefixed.group(1).strip()
            spans.append(prefixed.span())
        else:
            cadence = _find_cron(text, spans)
    next_run = _take(_NEXT, text, spans)
    paused = _PAUSED.search(text) is not None
    if paused:
        found = _PAUSED.search(text)
        if found is not None:
            spans.append(found.span())
    instruction = _remainder(text, spans)
    return JobDraft(
        workspace=workspace,
        instruction=instruction or None,
        cadence=cadence,
        next_run=next_run,
        deliver_to=deliver,
        paused=paused,
    )


def _take(pattern: re.Pattern[str], text: str, spans: list[tuple[int, int]]) -> str | None:
    match = pattern.search(text)
    if match is None:
        return None
    spans.append(match.span())
    if match.lastindex:
        return match.group(1)
    return match.group(0)


def _find_cron(text: str, spans: list[tuple[int, int]]) -> str | None:
    tokens = list(re.finditer(r"\S+", text))
    for index in range(len(tokens) - 4):
        window = tokens[index : index + 5]
        if all(_CRON_FIELD.fullmatch(part.group(0)) for part in window):
            spans.append((window[0].start(), window[4].end()))
            return " ".join(part.group(0) for part in window)
    return None


def _remainder(text: str, spans: list[tuple[int, int]]) -> str:
    chunks: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        chunks.append(text[cursor:start])
        cursor = end
    chunks.append(text[cursor:])
    leftover = " ".join("".join(chunks).split())
    leftover = _LEAD_RUN.sub("", leftover)
    leftover = leftover.strip(" ,.;")
    leftover = re.sub(r"\s+\b(?:and|then|to)\s*$", "", leftover, flags=re.IGNORECASE)
    return leftover.strip(" ,.;")


def _deliver_to(raw: str | None) -> DeliverTo | None:
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _DELIVER_TO:
        return value
    return None


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "paused"}
