"""Workspace instruction files for the project home (TD-2802).

Human path: list the workspace's steering files, and create a new
``.tst/rules/`` file. The agent tools never write these paths (TD-2102).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .discover import SteeringFileResolver

InstructionKind = Literal["agents", "claude", "rule"]

_RULE_STEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RESERVED_STEMS = frozenset({"agents", "claude"})

RULE_TEMPLATE = """\
<!--
A path-scoped rule. Add appliesTo in the frontmatter to limit it, or
leave it unconditional. Standing project conventions belong in AGENTS.md.
See docs/steering.md.
-->
"""


class InstructionNameError(ValueError):
    """The proposed rule name is not a legal ``.tst/rules/`` basename."""


@dataclass(frozen=True)
class InstructionFile:
    """One file the Instructions column lists."""

    path: Path
    name: str
    kind: InstructionKind


def list_workspace_instructions(workspace: str | Path) -> list[InstructionFile]:
    """Workspace-root steering plus ``.tst/rules/*.md``.

    Nested and user-global files stay off this column — they are not
    the project's Instructions list (spec §4 / TD-2802).
    """
    root = Path(workspace)
    files: list[InstructionFile] = []
    chosen = SteeringFileResolver._agents_or_claude(root)
    if chosen is not None:
        path, is_fallback, _shadowed = chosen
        files.append(
            InstructionFile(
                path=path,
                name=path.name,
                kind="claude" if is_fallback else "agents",
            )
        )
    rules_dir = root / ".tst" / "rules"
    if rules_dir.is_dir():
        for path in sorted(rules_dir.glob("*.md")):
            if path.is_file():
                files.append(InstructionFile(path=path, name=path.name, kind="rule"))
    return files


def normalize_rule_name(name: str) -> str:
    """Turn a typed name into a ``.md`` basename, or raise."""
    stem = name.strip()
    if stem.lower().endswith(".md"):
        stem = stem[:-3]
    if not _RULE_STEM.match(stem):
        raise InstructionNameError("rule name must be letters, digits, dot, underscore, or hyphen")
    if stem.lower() in _RESERVED_STEMS:
        raise InstructionNameError("AGENTS.md and CLAUDE.md are not rules files")
    return f"{stem}.md"


def create_rule_file(workspace: str | Path, name: str) -> Path:
    """Plant a commented ``.tst/rules/<name>.md``. Never overwrites.

    Returns the path (existing or newly written). Raises
    ``InstructionNameError`` for a bad name, ``FileNotFoundError`` if
    *workspace* is not a directory.
    """
    root = Path(workspace)
    if not root.is_dir():
        raise FileNotFoundError(f"Workspace path is not a directory: {root}")
    filename = normalize_rule_name(name)
    dest = root / ".tst" / "rules" / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_text(RULE_TEMPLATE, encoding="utf-8")
    return dest
