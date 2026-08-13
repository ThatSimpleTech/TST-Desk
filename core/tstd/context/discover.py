"""Steering file discovery — find steering files in precedence order.

Spec §4.1: loaded at session start, lowest → highest precedence:

    | Scope        | Location                      |
    |--------------|-------------------------------|
    | User global  | ``~/.tstdesk/AGENTS.md``      |
    | Workspace    | ``<workspace>/AGENTS.md``     |
    | Rules dir    | ``<workspace>/.tst/rules/*.md`` |
    | Directory    | ``<workspace>/**/AGENTS.md``  |

The resolver returns *existing* files only, so missing files are not
errors — they simply never appear.  Nested ``AGENTS.md`` files carry
their workspace-relative subtree so consumers can scope them: a nested
file applies to its subtree only, and a deeper file overrides a
shallower one within the nested level.

Discovery does filesystem I/O synchronously (matching ``config.py``).
Async callers wrap it in ``asyncio.to_thread`` — see
``ContextAssembler.assemble``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

# Directories never searched for nested AGENTS.md: VCS metadata and
# per-workspace runtime state. Everything else is fair game — steering
# files can legitimately live anywhere in a repo tree.
_SKIP_DIRS = {".git", ".tst", ".tstdesk", "__pycache__"}


class Precedence(IntEnum):
    """Steering precedence — higher value overrides lower (spec §4.1)."""

    USER_GLOBAL = 0
    WORKSPACE = 1
    RULES = 2
    NESTED = 3

    @property
    def label(self) -> str:
        """Human-readable scope label for provenance comments."""
        return _LABELS[self]


_LABELS: dict[Precedence, str] = {
    Precedence.USER_GLOBAL: "user global",
    Precedence.WORKSPACE: "workspace",
    Precedence.RULES: "rules",
    Precedence.NESTED: "nested",
}


@dataclass(frozen=True)
class SteeringSource:
    """A steering file found during discovery.

    Attributes:
        path: Absolute path to the steering file.
        precedence: Precedence level (higher overrides lower).
        subtree: For nested ``AGENTS.md`` files, the workspace-relative
            directory the file applies to (e.g. ``"src/api"``), or
            ``None`` for non-nested sources.
    """

    path: Path
    precedence: Precedence
    subtree: str | None = None


class SteeringFileResolver:
    """Discovers steering files for a workspace in precedence order.

    The result is ordered lowest → highest precedence so callers can
    concatenate with later files overriding earlier ones.
    """

    def __init__(self, home_dir: str | Path | None = None) -> None:
        """Create a resolver.

        Args:
            home_dir: Override the user home directory (test seam).
                Defaults to the real user home.
        """
        self._home = Path(home_dir).expanduser() if home_dir is not None else Path.home()

    def resolve(self, workspace_path: str | Path) -> list[SteeringSource]:
        """Discover steering files for *workspace_path*.

        Returns existing files only, ordered lowest → highest
        precedence: user global, workspace root, rules dir, then nested
        files shallowest-first (deeper files override shallower ones).

        Missing files are not errors — they simply do not appear.
        """
        workspace = Path(workspace_path).expanduser().resolve()
        sources: list[SteeringSource] = []

        # 1. User global — personal preferences across all workspaces.
        global_path = self._home / ".tstdesk" / "AGENTS.md"
        if global_path.exists():
            sources.append(SteeringSource(path=global_path, precedence=Precedence.USER_GLOBAL))

        # 2. Workspace root — team conventions, git-tracked.
        root_path = workspace / "AGENTS.md"
        if root_path.exists():
            sources.append(SteeringSource(path=root_path, precedence=Precedence.WORKSPACE))

        # 3. Rules dir — modular rules, sorted by name for determinism.
        rules_dir = workspace / ".tst" / "rules"
        sources.extend(
            SteeringSource(path=rule_path, precedence=Precedence.RULES)
            for rule_path in sorted(rules_dir.glob("*.md"))
        )

        # 4. Nested — subtree-specific AGENTS.md, shallowest first so
        #    deeper (more specific) files come later and override.
        sources.extend(self._nested_sources(workspace))

        return sources

    def _nested_sources(self, workspace: Path) -> list[SteeringSource]:
        """Find nested AGENTS.md files below the workspace root.

        Skips ``_SKIP_DIRS`` (VCS metadata, runtime state) and does not
        follow directory symlinks, so discovery cannot loop or escape
        the workspace.  Returns sources ordered by depth (shallowest
        first), then by subtree path for determinism.
        """
        found: list[tuple[int, str, Path]] = []
        for root, dirs, files in os.walk(workspace, followlinks=False):
            # Prune skip dirs in place so os.walk does not descend into them.
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
            if "AGENTS.md" not in files:
                continue
            root_path = Path(root)
            if root_path == workspace:
                continue  # workspace root is handled as its own level
            rel = root_path.relative_to(workspace)
            subtree = str(rel) if rel.parts else "."
            found.append((len(rel.parts), subtree, root_path / "AGENTS.md"))

        found.sort(key=lambda item: (item[0], item[1]))
        return [
            SteeringSource(path=path, precedence=Precedence.NESTED, subtree=subtree)
            for _, subtree, path in found
        ]
