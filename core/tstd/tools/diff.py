"""Unified diffs for tool results (TD-604).

Every write reports the change it made: the dispatcher snapshots the
canonical write targets before and after the handler runs and renders a
unified diff onto the ``tool_result`` event for display.  Snapshots are
best-effort — absent, unreadable, oversized, or non-UTF-8 files simply
yield no diff rather than failing the write.
"""

from __future__ import annotations

import difflib
import os
import stat
from pathlib import Path

# Skip snapshotting files larger than this; the diff would be noise.
_MAX_SNAPSHOT_BYTES = 5_000_000


def snapshot_text(path: Path) -> str | None:
    """Best-effort UTF-8 content of *path* for diffing.

    ``None`` when the file does not exist (the diff renders as a new
    file) or when it is unreadable, oversized, or not UTF-8 text (no
    diff — a missing diff must never fail a write).
    """
    try:
        # Only a regular file: reading a FIFO or a device blocks, or never
        # ends. Opened without blocking and judged by fstat on the open
        # file, so a swap after a stat cannot slip a FIFO in (TD-5161).
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_SNAPSHOT_BYTES:
            return None
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(_MAX_SNAPSHOT_BYTES + 1)
        return None if len(data) > _MAX_SNAPSHOT_BYTES else data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def render_diff(before: str | None, after: str | None, label: str) -> str | None:
    """Unified diff of *before* → *after* under *label*.

    ``None`` when neither side is diffable; an empty string when the
    write changed nothing.
    """
    if before is None and after is None:
        return None
    lines = difflib.unified_diff(
        (before or "").splitlines(),
        (after or "").splitlines(),
        fromfile=f"a/{label}",
        tofile=f"b/{label}",
        lineterm="",
    )
    return "\n".join(lines)
