"""The change a write would make, shown on its approval card (APPROVE-03).

The card had a diff view that read ``arguments.diff``, which no tool has,
so it never showed: an ``fs_edit`` card led with an absolute path, the old
and new text sat in a collapsed JSON blob, and an ``fs_write`` card gave no
sign whether it created a file or overwrote one. ``fs_write`` and
``fs_edit`` are pure functions of the file and their arguments, so the
would-be result is computed here, before the call is parked, with the same
diff the tool result shows afterwards (TD-604).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .diff import render_diff, snapshot_text

#: The longest preview sent on a card; the full change is in the result.
PREVIEW_MAX_CHARS = 20_000


def _target(raw: object, workspace: Path) -> Path | None:
    if not isinstance(raw, str) or raw.strip() == "":
        return None
    path = Path(raw)
    return path if path.is_absolute() else workspace / path


def _label(target: Path, workspace: Path) -> str:
    try:
        return str(target.resolve().relative_to(workspace.resolve()))
    except (OSError, ValueError):
        return str(target)


def preview_write_diff(tool_name: str, arguments: dict[str, Any], workspace: Path) -> str | None:
    """The unified diff *tool_name* would write, or None when it cannot be known."""
    if tool_name not in ("fs_write", "fs_edit"):
        return None
    target = _target(arguments.get("path"), workspace)
    if target is None:
        return None
    before = snapshot_text(target)
    if tool_name == "fs_write":
        after = arguments.get("content")
        if not isinstance(after, str):
            return None
    else:
        old, new = arguments.get("old_string"), arguments.get("new_string")
        if before is None or not isinstance(old, str) or not isinstance(new, str) or old == "":
            return None
        if old not in before:
            return None  # the edit would fail; the tool says why
        count = -1 if arguments.get("replace_all") is True else 1
        after = before.replace(old, new, count)
    diff = render_diff(before, after, _label(target, workspace))
    if diff is None:
        return None
    if len(diff) > PREVIEW_MAX_CHARS:
        diff = diff[:PREVIEW_MAX_CHARS] + "\n… (the rest of the change is not shown here)"
    return diff
