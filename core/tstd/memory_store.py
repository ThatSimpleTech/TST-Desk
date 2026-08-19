"""Workspace memory files (TD-2101).

Spec §5: markdown under ``.tst/memory/``, human-readable, git-tracked.
Scaffold plants the three named files with commented templates. Topical
``<topic>.md`` files are allowed later; this module never invents names
beyond those three.
"""

from __future__ import annotations

from pathlib import Path

MEMORY_DIRNAME = Path(".tst") / "memory"

#: The three files spec §5 names. Distill or the user may add others;
#: scaffold never does.
MEMORY_FILENAMES: tuple[str, ...] = ("MEMORY.md", "decisions.md", "gotchas.md")

# HTML-comment templates so a freshly scaffolded tree is empty of facts
# a heading-match loader could treat as memory. Standing rules stay in
# AGENTS.md.
_TEMPLATES: dict[str, str] = {
    "MEMORY.md": """\
<!--
MEMORY.md — durable facts about this project.

Distill writes here; you correct it. Topic files live beside this one
as <topic>.md. The store does not invent those names until distill or
you do.

Standing rules belong in AGENTS.md, not here. Mixing them is how last
month's decisions get read as this month's rules.
-->
""",
    "decisions.md": """\
<!--
decisions.md — why things are the way they are.

Accepted choices, not standing rules. Rules go in AGENTS.md.
-->
""",
    "gotchas.md": """\
<!--
gotchas.md — things that bit us.

Sharp edges, not instructions. Instructions go in AGENTS.md.
-->
""",
}


def memory_dir(workspace: str | Path) -> Path:
    """``<workspace>/.tst/memory``."""
    return Path(workspace) / MEMORY_DIRNAME


class MemoryCapError(Exception):
    """A memory write would exceed the line cap, or replace a file at cap."""


def count_lines(text: str) -> int:
    """Line count for the cap: ``splitlines``, empty file is zero."""
    if text == "":
        return 0
    return len(text.splitlines())


def path_is_memory_file(path: Path, workspace_root: Path | None) -> bool:
    """Whether *path* is a memory markdown file (not a steering basename)."""
    if workspace_root is not None:
        from .autonomy.classifier import Boundary, is_memory_write

        return is_memory_write(Boundary(workspace_root=workspace_root), path)
    parts = path.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".tst" and parts[i + 1] == "memory":
            return path.name.upper() not in {"AGENTS.MD", "CLAUDE.MD"}
    return False


def memory_max_lines(session: object | None) -> int:
    """The workspace's ``memory.max_lines``, or the shipped default."""
    cfg = getattr(session, "boundary_config", None) if session is not None else None
    memory = getattr(cfg, "memory", None) if cfg is not None else None
    if memory is not None:
        return int(memory.max_lines)
    from .boundary_config import DEFAULT_MEMORY_MAX_LINES

    return DEFAULT_MEMORY_MAX_LINES


def check_memory_write(
    path: Path,
    proposed: str,
    max_lines: int,
    *,
    replace_at_cap: bool = False,
) -> None:
    """Refuse a tool write that exceeds the cap or replaces a file at cap.

    Distill (E23) passes ``replace_at_cap=True`` so it may replace a file
    that is already at the cap. It still cannot write more than *max_lines*.
    """
    existing = 0
    if path.exists() and path.is_file():
        existing = count_lines(path.read_text(encoding="utf-8"))
    if existing >= max_lines and not replace_at_cap:
        raise MemoryCapError(
            f"{path.name} is already at the {max_lines}-line cap. Distill it, do not append."
        )
    n = count_lines(proposed)
    if n > max_lines:
        raise MemoryCapError(
            f"this write would make {path.name} {n} lines; the cap is {max_lines}. "
            "Distill the file, do not append."
        )


def replace_memory_file(path: Path, content: str, max_lines: int) -> None:
    """Distill (E23) write: may replace a file at cap; still cannot exceed it."""
    check_memory_write(path, content, max_lines, replace_at_cap=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def scaffold_workspace_memory(workspace: str | Path) -> list[Path]:
    """Plant commented templates when missing. Never overwrites.

    Creates ``.tst/memory/`` if needed. Each of the three named files is
    written only when that path does not already exist, so a second open
    (or a user-edited file) is left alone. Returns the paths written —
    empty when everything already existed.
    """
    root = memory_dir(workspace)
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in MEMORY_FILENAMES:
        path = root / name
        if path.exists():
            continue
        path.write_text(_TEMPLATES[name], encoding="utf-8")
        written.append(path)
    return written
