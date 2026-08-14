"""Unified diffs for tool results (TD-604).

Every write reports the change it made: the dispatcher snapshots the
canonical write targets before and after the handler runs and renders a
unified diff onto the ``tool_result`` event for display.  Snapshots are
best-effort — absent, unreadable, oversized, or non-UTF-8 files simply
yield no diff rather than failing the write.
"""

from __future__ import annotations

import difflib
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
        if path.stat().st_size > _MAX_SNAPSHOT_BYTES:
            return None
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


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
