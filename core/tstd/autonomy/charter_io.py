"""Human-path charter read/write (TD-4002).

The project-home pane writes ``.tst/autonomy/CHARTER.md`` as the human.
The agent cannot: the classifier already treats that path as steering
(Class C).  This module validates with :func:`parse_charter`, walls
``source_of_truth`` through ``PathGuard.check_read`` (the same check the
tools use), and writes the file.  It does not commit — signing is
TD-4003.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .charter import (
    CHARTER_RELATIVE_PARTS,
    MAX_CHARTER_BYTES,
    Charter,
    CharterError,
    charter_notes,
    charter_path,
    parse_charter,
)
from .classifier import Boundary, canonical_path, is_in_workspace


def render_charter_markdown(data: dict[str, Any], notes: str = "") -> str:
    """Serialize *data* as the §12.4 frontmatter form.

    The mapping is dumped as-is so :func:`parse_charter` is the schema —
    unknown keys and missing fields fail there, not in a second model.
    """
    dumped = yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    if not dumped.endswith("\n"):
        dumped += "\n"
    text = f"---\n{dumped}---\n"
    stripped = notes.strip()
    if stripped:
        text += f"\n{stripped}\n"
    return text


def assert_sources_walled(workspace: Path, charter: Charter) -> None:
    """Refuse ``source_of_truth`` entries that escape *workspace*.

    ``PathGuard.check_read`` is the wall: absolute paths, ``..``,
    Windows-unsafe forms, and symlink-out all refuse.  A string-prefix
    check would miss a symlink.
    """
    from ..tools.boundary import PathGuard, RefusalError

    guard = PathGuard(Boundary(workspace_root=Path(workspace).resolve()))
    for raw in charter.source_of_truth:
        try:
            guard.check_read(raw)
        except RefusalError as e:
            raise CharterError(f"source_of_truth: {e.reason}") from e


def _write_path(workspace: Path) -> Path:
    """The charter path, after confirming the write cannot leave *workspace*."""
    root = Path(workspace).resolve()
    boundary = Boundary(workspace_root=root)
    cursor = root
    for part in CHARTER_RELATIVE_PARTS[:-1]:
        nxt = cursor / part
        if nxt.exists() or nxt.is_symlink():
            resolved = canonical_path(nxt)
            if not is_in_workspace(boundary, resolved):
                raise CharterError(f"{nxt} resolves outside the workspace — refusing to write")
            cursor = resolved
            continue
        nxt.mkdir(exist_ok=True)
        resolved = canonical_path(nxt)
        if not is_in_workspace(boundary, resolved):
            raise CharterError(f"{nxt} resolves outside the workspace — refusing to write")
        cursor = resolved
    dest = cursor / CHARTER_RELATIVE_PARTS[-1]
    if dest.exists() or dest.is_symlink():
        target = canonical_path(dest)
        if not is_in_workspace(boundary, target):
            raise CharterError(f"{dest} resolves outside the workspace — refusing to write")
    return dest


def read_charter_document(workspace: Path) -> tuple[bool, Charter | None, str]:
    """Load the workspace charter, or ``(False, None, "")`` when absent.

    Raises:
        CharterError: Unreadable, oversized, or invalid — the message
            names the offending field.
    """
    path = charter_path(workspace)
    if not path.is_file():
        return False, None, ""
    try:
        if path.stat().st_size > MAX_CHARTER_BYTES:
            raise CharterError(
                f"{path} is larger than {MAX_CHARTER_BYTES} bytes — "
                "that is not a charter; refusing to parse it"
            )
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise CharterError(f"Failed to read {path}: {e}") from e
    return True, parse_charter(text, source=path), charter_notes(text)


def write_charter_document(
    workspace: Path,
    data: dict[str, Any],
    notes: str = "",
) -> tuple[Charter, str]:
    """Validate, wall ``source_of_truth``, write. Does not commit.

    Raises:
        CharterError: Invalid shape (field named), SoT escape, oversized,
            or a destination that resolves outside the workspace.
    """
    text = render_charter_markdown(data, notes)
    if len(text.encode("utf-8")) > MAX_CHARTER_BYTES:
        raise CharterError(
            f"charter is larger than {MAX_CHARTER_BYTES} bytes — refusing to write it"
        )
    dest = charter_path(workspace)
    charter = parse_charter(text, source=dest)
    assert_sources_walled(workspace, charter)
    path = _write_path(workspace)
    path.write_text(text, encoding="utf-8")
    return charter, charter_notes(text)
