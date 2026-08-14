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
from pathlib import Path


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


def _fs_write(path: Path, content: str, append: bool) -> str:
    existed = path.exists()
    old = ""
    if append and existed:
        old = path.read_text(encoding="utf-8")
    _atomic_write(path, old + content)
    verb = "appended to" if append else ("overwrote" if existed else "created")
    return f"wrote {len(content.encode('utf-8'))} bytes; {verb} {path}"


def _fs_edit(path: Path, old_string: str, new_string: str) -> str:
    if not path.exists():
        raise FileNotFoundError(f"'{path}' does not exist — use fs_write to create it")
    if path.is_dir():
        raise ValueError(f"'{path}' is a directory, not a file")
    content = path.read_text(encoding="utf-8")
    count = content.count(old_string)
    if count == 0:
        raise ValueError(f"target string not found in '{path}' — nothing was changed")
    if count > 1:
        raise ValueError(
            f"target string is ambiguous: {count} occurrences in '{path}' — "
            "include more surrounding context so exactly one matches"
        )
    _atomic_write(path, content.replace(old_string, new_string, 1))
    return f"edited {path}: replaced 1 occurrence"


async def fs_write(session: object, path: str, content: str, append: bool = False) -> str:
    """Create or overwrite a file; parent directories created (TD-604)."""
    return await asyncio.to_thread(_fs_write, Path(path), content, append)


async def fs_edit(session: object, path: str, old_string: str, new_string: str) -> str:
    """Exact single-occurrence string replacement in a file (TD-604)."""
    return await asyncio.to_thread(_fs_edit, Path(path), old_string, new_string)
