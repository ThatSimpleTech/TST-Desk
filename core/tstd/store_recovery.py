"""When a store file cannot be read, keep it and recover what can be (ERR2B-04).

sessions.json and scheduler/jobs.json were each read whole and believed: one
truncation or bad byte and the app started with no chats or no schedules,
said nothing, and the next save overwrote the file for good. Now:

- the unreadable file is moved to ``<name>.damaged-<UTC stamp>`` and never
  written again;
- every save first keeps the file it replaces as ``<name>.bak``, which a
  failed load falls back to;
- the chat list is rebuilt from the session folders it does not name
  (``sessions/<id>/``), as far as their own files say where they belong;
- what happened is kept as a ``Recovery`` for the daemon to tell the windows.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .logging import get_logger

log = get_logger("tstd.store_recovery")


@dataclass(frozen=True)
class Recovery:
    """One store that could not be read, and what came back."""

    store: str  # "sessions", "jobs" or "audit"
    kept_as: str  # the damaged file's new name, beside the store
    from_backup: int  # rows read back from <name>.bak
    rebuilt: int  # session rows rebuilt from their folders
    error: str
    #: The store could not be opened at all; this run keeps it in memory (ERR2B-06).
    memory_only: bool = False


def stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.bak")


def keep_backup(path: Path) -> None:
    """Before a save replaces *path*, keep it as ``<name>.bak`` (owner-only)."""
    if not path.exists():
        return
    bak = backup_path(path)
    tmp = bak.with_name(f".{bak.name}.tmp")
    try:
        shutil.copyfile(path, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, bak)
    except OSError as e:
        with contextlib.suppress(OSError):
            tmp.unlink()
        fields = {"path": str(path), "error": str(e)}
        log.warning("could not keep a backup", extra={"extra_fields": fields})


def set_aside(path: Path) -> Path | None:
    """Move an unreadable *path* to ``<name>.damaged-<stamp>``; the new path, or None."""
    target = path.with_name(f"{path.name}.damaged-{stamp()}")
    try:
        os.replace(path, target)
        os.chmod(target, 0o600)
    except OSError as e:
        fields = {"path": str(path), "error": str(e)}
        log.warning("could not set the damaged file aside", extra={"extra_fields": fields})
        return None
    log.warning("store file unreadable; kept aside", extra={"extra_fields": {"path": str(target)}})
    return target


def read_backup(path: Path) -> Any | None:
    """The parsed ``<name>.bak`` of *path*, or None when it is missing or unreadable."""
    bak = backup_path(path)
    try:
        return json.loads(bak.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── Rebuilding the chat list from the session folders ────────────────────


def _first_user_text(conversation: Any) -> str:
    if not isinstance(conversation, list):
        return ""
    for message in conversation:
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _workspace_from(conversation: Any) -> str | None:
    """The ``Workspace root:`` line the system prompt carries (TD-1810)."""
    from .context.prompt import WORKSPACE_ROOT_LABEL

    if not isinstance(conversation, list):
        return None
    for message in conversation:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        for line in content.splitlines():
            if line.startswith(WORKSPACE_ROOT_LABEL):
                root = line[len(WORKSPACE_ROOT_LABEL) :].strip()
                if root.startswith("/") or (len(root) > 2 and root[1] == ":"):
                    return root
    return None


def folder_rows(sessions_dir: Path, known: set[str]) -> list[dict[str, Any]]:
    """Rows for session folders the list does not name, where the folder says enough.

    A folder is rebuilt only when its conversation names its workspace; the
    row is ``idle`` and titled from its first message.
    """
    from .session_store import title_from_user_message

    rows: list[dict[str, Any]] = []
    if not sessions_dir.is_dir():
        return rows
    for folder in sorted(sessions_dir.iterdir()):
        if not folder.is_dir() or folder.name in known:
            continue
        try:
            conversation = json.loads((folder / "conversation.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        workspace = _workspace_from(conversation)
        if workspace is None:
            continue
        title = title_from_user_message(_first_user_text(conversation))
        modified = datetime.fromtimestamp(folder.stat().st_mtime, UTC).isoformat()
        rows.append(
            {
                "session_id": folder.name,
                "workspace_path": workspace,
                "state": "idle",
                "created_at": modified,
                "updated_at": modified,
                "title": title,
                "auto_title": title,
            }
        )
    return rows
