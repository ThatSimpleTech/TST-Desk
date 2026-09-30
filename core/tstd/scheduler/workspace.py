"""Resolve and check a new job's workspace before it is saved.

Kept out of the model validator on purpose: ``Job`` also loads old rows from
disk, and a job whose folder has since moved must still load so it can be
paused or deleted. These checks run only when a job is *created*.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .models import JobValidationError


def resolve_workspace_name(raw: str, known: Iterable[str]) -> str:
    """Turn a bare folder name into the one known workspace it names.

    People type "Client Reports", not a path. If exactly one workspace the daemon
    already knows has that basename (case-insensitive) use it; on zero or
    several matches return the text unchanged so validation explains the
    problem instead of this guessing.
    """
    text = raw.strip()
    if not text or "/" in text or "\\" in text or Path(text).expanduser().is_absolute():
        return raw
    wanted = text.casefold()
    matches = {path for path in known if Path(path).name.casefold() == wanted}
    if len(matches) == 1:
        return next(iter(matches))
    return raw


def require_folder(workspace: str) -> None:
    """Refuse a workspace that is not an existing directory."""
    if not Path(workspace).is_dir():
        raise JobValidationError(f"Workspace: folder not found: {workspace}")
