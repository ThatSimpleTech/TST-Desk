"""Built-in tool handlers — filesystem write tools (TD-604).

Writes are atomic — a temp file in the target's directory plus
``os.replace`` — so a failed write never leaves a partial file behind.
Handlers consume paths the boundary guard (TD-602) has already
canonicalized and checked, and fail loudly by raising: dispatch turns
the exception into a structured ``handler_error`` result, which also
keeps a failed write from being checkpointed (TD-705).

Handlers are thin async wrappers: the blocking filesystem work runs in a
worker thread via ``asyncio.to_thread`` so the event loop never stalls
(AGENTS.md §6).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _atomic_write(target: Path, content: str) -> None:
    """Write *content* to *target* atomically: temp file plus rename.

    The temp file is created in the target's own directory so the rename
    is guaranteed to be atomic (same filesystem).  Parent directories are
    created as needed.  On any failure the temp file is removed and the
    previous target is left untouched.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _workspace_root(session: object | None) -> Path | None:
    raw = getattr(session, "workspace_path", None) if session is not None else None
    return Path(raw) if isinstance(raw, str) and raw else None


def _enforce_memory_cap(path: Path, proposed: str, session: object | None) -> None:
    """Refuse a memory-file write that the line cap forbids (TD-2103)."""
    from ..memory_store import check_memory_write, memory_max_lines, path_is_memory_file

    if not path_is_memory_file(path, _workspace_root(session)):
        return
    check_memory_write(path, proposed, memory_max_lines(session))


def _fs_write(path: Path, content: str, append: bool, session: object | None) -> str:
    existed = path.exists()
    old = ""
    if append and existed:
        old = path.read_text(encoding="utf-8")
    proposed = old + content
    _enforce_memory_cap(path, proposed, session)
    _atomic_write(path, proposed)
    verb = "appended to" if append else ("overwrote" if existed else "created")
    return f"wrote {len(content.encode('utf-8'))} bytes; {verb} {path}"


#: Most replacements one ``fs_edit`` call may carry (TD-5270).
MAX_EDITS = 20


@dataclass(frozen=True)
class Edit:
    """One replacement: an exact string, its replacement, and whether every
    occurrence goes or exactly one must exist."""

    old_string: str
    new_string: str
    replace_all: bool = False


def edits_from_arguments(
    old_string: str | None,
    new_string: str | None,
    replace_all: bool,
    edits: list[dict[str, Any]] | None,
) -> list[Edit]:
    """The call's replacements, from whichever of its two forms it used.

    One pair at the top level, or a list under ``edits``: exactly one of the
    two. Anything else is the caller's mistake and changes nothing.
    """
    single = old_string is not None or new_string is not None
    if edits is not None and single:
        raise ValueError("give old_string and new_string, or edits, not both")
    if edits is None:
        if old_string is None or new_string is None:
            raise ValueError("give old_string and new_string, or a list of edits")
        return [Edit(old_string, new_string, replace_all)]
    if not edits:
        raise ValueError("edits is empty — nothing to change")
    if len(edits) > MAX_EDITS:
        raise ValueError(f"too many edits: {len(edits)} (at most {MAX_EDITS} in one call)")
    out: list[Edit] = []
    for index, item in enumerate(edits, start=1):
        old, new = item.get("old_string"), item.get("new_string")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError(f"edit {index} needs old_string (not empty) and new_string")
        out.append(Edit(old, new, bool(item.get("replace_all", False))))
    return out


def apply_edits(content: str, edits: list[Edit], where: str) -> tuple[str, list[int]]:
    """*content* after *edits*, in order, and how many places each changed.

    Each edit sees the text the one before it left. The first that cannot
    apply stops everything, and names its place in the list when there is
    more than one, so the caller writes nothing.
    """
    counts: list[int] = []
    for index, edit in enumerate(edits, start=1):
        which = f"edit {index}: " if len(edits) > 1 else ""
        found = content.count(edit.old_string)
        if found == 0:
            raise ValueError(f"{which}target string not found in '{where}' — nothing was changed")
        if found > 1 and not edit.replace_all:
            raise ValueError(
                f"{which}target string is ambiguous: {found} occurrences in '{where}' — "
                "include more surrounding context so exactly one matches, or set "
                "replace_all — nothing was changed"
            )
        content = content.replace(edit.old_string, edit.new_string)
        counts.append(found)
    return content, counts


def _fs_edit(path: Path, edits: list[Edit], session: object | None) -> str:
    if not path.exists():
        raise FileNotFoundError(f"'{path}' does not exist — use fs_write to create it")
    if path.is_dir():
        raise ValueError(f"'{path}' is a directory, not a file")
    proposed, counts = apply_edits(path.read_text(encoding="utf-8"), edits, str(path))
    _enforce_memory_cap(path, proposed, session)
    # One write for the whole call: a batch is one change to the file.
    _atomic_write(path, proposed)
    if len(counts) == 1:
        return f"edited {path}: replaced {_occurrences(counts[0])}"
    each = ", ".join(str(n) for n in counts)
    return f"edited {path}: {len(counts)} edits, replaced {each} ({_occurrences(sum(counts))})"


def _occurrences(count: int) -> str:
    return "1 occurrence" if count == 1 else f"{count} occurrences"


async def fs_write(
    session: object, path: str, content: str, append: bool = False, tool_call_id: str = ""
) -> str:
    """Create or overwrite a file; parent directories created (TD-604)."""
    return await asyncio.to_thread(_fs_write, Path(path), content, append, session)


async def fs_edit(
    session: object,
    path: str,
    old_string: str | None = None,
    new_string: str | None = None,
    replace_all: bool = False,
    edits: list[dict[str, Any]] | None = None,
    tool_call_id: str = "",
) -> str:
    """Exact string replacement in one file (TD-604): one, every occurrence
    of one, or several in order with nothing written if any fails (TD-5270)."""
    wanted = edits_from_arguments(old_string, new_string, replace_all, edits)
    return await asyncio.to_thread(_fs_edit, Path(path), wanted, session)
