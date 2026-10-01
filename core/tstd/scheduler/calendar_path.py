"""Normalize a job's local calendar path and match list (TD-3818).

The path rules match a workspace: no credentials, not an address, absolute
after ``expanduser``, and ``normpath`` without resolving links. Existence
is separate, the same way a moved workspace folder still loads. This
module does not import the job model, so the model can call it.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..logging import redact_secrets

_MATCH_CAP = 200


class CalendarPathError(ValueError):
    """The path or the match list cannot be stored."""


def normalize_skip_calendar(raw: str | None) -> str | None:
    """Absolute ``.ics`` path, or None when the field is blank."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if redact_secrets(text) != text:
        raise CalendarPathError("must not contain secrets")
    if "\x00" in text or "://" in text:
        raise CalendarPathError("must be a filesystem path")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise CalendarPathError("must be a full file path")
    normal = os.path.normpath(str(path))
    if not normal.lower().endswith(".ics"):
        raise CalendarPathError("must be a .ics file")
    return normal


def normalize_skip_match(raw: str | None) -> str | None:
    """``holiday|PTO`` with blanks dropped, or None when nothing remains.

    Case is kept. Matching later folds case. A list of only separators
    is the same as blank: every event matches.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if redact_secrets(text) != text:
        raise CalendarPathError("must not contain secrets")
    if len(text) > _MATCH_CAP:
        raise CalendarPathError("is too long")
    parts = [part.strip() for part in text.split("|")]
    kept = [part for part in parts if part]
    if not kept:
        return None
    return "|".join(kept)


def require_calendar_file(path: str) -> None:
    """The file must exist when the path is newly set. A directory does not."""
    if not Path(path).is_file():
        raise CalendarPathError(f"Skip calendar: file not found: {path}")
