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
