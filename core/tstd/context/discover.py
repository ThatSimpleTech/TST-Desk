"""Steering file discovery — find steering files in precedence order.

Spec §4.1: loaded at session start, lowest → highest precedence:

    | Scope        | Location                      |
    |--------------|-------------------------------|
    | User global  | ``~/.tstdesk/AGENTS.md``      |
    | Workspace    | ``<workspace>/AGENTS.md``     |
    | Rules dir    | ``<workspace>/.tst/rules/*.md`` |
    | Directory    | ``<workspace>/**/AGENTS.md``  |

**CLAUDE.md fallback (TD-502, TD-4845).** Inside the workspace, if
``AGENTS.md`` is absent and ``CLAUDE.md`` is present, ``CLAUDE.md`` is
used.  If both are present, ``AGENTS.md`` wins and the shadowing is
recorded in ``shadowed_path``.  The user-global level reads
``~/.tstdesk/AGENTS.md`` only, unless ``claude_global_fallback`` is
true, in which case ``~/.claude/CLAUDE.md`` is the last resort.  When
the flag is false and that Claude file exists with no
``~/.tstdesk/AGENTS.md``, :meth:`SteeringFileResolver.claude_global_notice`
names it for Doctor.  The file is not opened, and neither is anything
it imports.

The resolver returns *existing* files only, so missing files are not
errors — they simply never appear.  Nested steering files carry their
workspace-relative subtree so consumers can scope them: a file applies
to its subtree only, and a deeper file overrides a shallower one.

Discovery does filesystem I/O synchronously (matching ``config.py``).
Async callers wrap it in ``asyncio.to_thread`` — see
``ContextAssembler.assemble``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

# Directories never searched for nested steering files: VCS metadata,
# per-workspace runtime state, and known tool convention directories.
# Everything else is fair game — steering files can legitimately live
# anywhere in a repo tree.
_SKIP_DIRS = {
    ".git",
    ".tst",
    ".tstdesk",
    ".claude",
    "__pycache__",
    "node_modules",
    ".venv",
    "dist",
    "build",
    "target",
}


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

# Doctor quotes this when Claude Code's global file is present and the
# opt-in is off.  The path is the tilde form so the row never carries
# an absolute home directory (TD-1104).
CLAUDE_GLOBAL_NOT_LOADED = (
    "~/.claude/CLAUDE.md not loaded — enable steering.claude_global_fallback to use it"
)


def append_steering_notice(detail: str, notices: tuple[str, ...]) -> str:
    """Attach discovery notes to one doctor detail, once."""
    if not notices:
        return detail
    return f"{detail}; {'; '.join(notices)}"


@dataclass(frozen=True)
class SteeringSource:
    """A steering file found during discovery.

    Attributes:
        path: Absolute path to the steering file.
        precedence: Precedence level (higher overrides lower).
        subtree: For nested files, the workspace-relative directory the
            file applies to (e.g. ``"src/api"``), or ``None`` for
            non-nested sources.
        is_fallback: ``True`` when this source is a ``CLAUDE.md`` used
            because ``AGENTS.md`` is absent at the same path.
        shadowed_path: When this source is an ``AGENTS.md`` that
            outranks a present ``CLAUDE.md`` at the same location, this
            field holds the path of the shadowed ``CLAUDE.md``.  ``None``
            when no shadowing occurs.
    """

    path: Path
    precedence: Precedence
    subtree: str | None = None
    is_fallback: bool = False
    shadowed_path: Path | None = None


class SteeringFileResolver:
    """Discovers steering files for a workspace in precedence order.

    The result is ordered lowest → highest precedence so callers can
    concatenate with later files overriding earlier ones.
    """

    def __init__(
        self,
        home_dir: str | Path | None = None,
        *,
        claude_global_fallback: bool = False,
    ) -> None:
        """Create a resolver.

        Args:
            home_dir: Override the user home directory (test seam).
                Defaults to the real user home.
            claude_global_fallback: When true, ``~/.claude/CLAUDE.md``
                is the user-global file if ``~/.tstdesk/AGENTS.md`` is
                absent (TD-502).  When false (the default, TD-4845),
                that file is never opened.  Workspace ``CLAUDE.md``
                fallback is not affected.
        """
        self._home = Path(home_dir).expanduser() if home_dir is not None else Path.home()
        self._claude_global_fallback = claude_global_fallback

    @property
    def home_dir(self) -> Path:
        """The home directory used for global-steering and ~ expansion.

        Exposed for import resolution (TD-504) to share the same test
        seam as discovery.
        """
        return self._home

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
        #    ~/.claude/CLAUDE.md is opt-in (TD-4845); see _user_global.
        global_source = self._user_global()
        if global_source is not None:
            sources.append(global_source)

        # 2. Workspace root — team conventions, git-tracked.
        root_result = self._agents_or_claude(workspace)
        if root_result is not None:
            sources.append(
                SteeringSource(
                    path=root_result[0],
                    precedence=Precedence.WORKSPACE,
                    is_fallback=root_result[1],
                    shadowed_path=root_result[2],
                )
            )

        # 3. Rules dir — modular rules, sorted by name for determinism.
        # ``.tst/memory/`` is a different tree (TD-2105) and is never
        # listed here; nested walk also skips ``.tst``.
        rules_dir = workspace / ".tst" / "rules"
        sources.extend(
            SteeringSource(path=rule_path, precedence=Precedence.RULES)
            for rule_path in sorted(rules_dir.glob("*.md"))
        )

        # 4. Nested — subtree-specific steering files, shallowest first
        #    so deeper (more specific) files come later and override.
        sources.extend(self._nested_sources(workspace))

        return sources

    def claude_global_notice(self) -> str | None:
        """The Doctor note when Claude Code's global file is left unread.

        Stats the path only when ``~/.tstdesk/AGENTS.md`` is absent and
        the fallback is off.  Does not open the file.  ``None`` when
        the file is loaded, shadowed by our own global file, or absent.
        """
        if self._claude_global_fallback:
            return None
        if (self._home / ".tstdesk" / "AGENTS.md").exists():
            return None
        if not (self._home / ".claude" / "CLAUDE.md").exists():
            return None
        return CLAUDE_GLOBAL_NOT_LOADED

    def _user_global(self) -> SteeringSource | None:
        """The user-global steering file, or ``None`` when there is none.

        ``~/.tstdesk/AGENTS.md`` always wins.  ``~/.claude/CLAUDE.md``
        is consulted only when ``claude_global_fallback`` is true: as
        the file itself when ours is absent, or as ``shadowed_path``
        when both exist.  With the flag off that path is not a
        candidate, so it is not statted and not recorded as shadowed.
        """
        agents = self._home / ".tstdesk" / "AGENTS.md"
        if agents.exists():
            shadowed: Path | None = None
            if self._claude_global_fallback:
                claude = self._home / ".claude" / "CLAUDE.md"
                shadowed = claude if claude.exists() else None
            return SteeringSource(
                path=agents,
                precedence=Precedence.USER_GLOBAL,
                shadowed_path=shadowed,
            )
        if not self._claude_global_fallback:
            return None
        claude = self._home / ".claude" / "CLAUDE.md"
        if not claude.exists():
            return None
        return SteeringSource(
            path=claude,
            precedence=Precedence.USER_GLOBAL,
            is_fallback=True,
        )

    @staticmethod
    def _agents_or_claude(
        directory: Path,
    ) -> tuple[Path, bool, Path | None] | None:
        """Resolve one directory's steering file.

        Returns ``(chosen_path, is_fallback, shadowed_path)`` or
        ``None`` when neither ``AGENTS.md`` nor ``CLAUDE.md`` exists.

        * ``AGENTS.md`` wins when both are present; ``shadowed_path``
          records the ``CLAUDE.md``.
        * If only ``CLAUDE.md`` exists, ``is_fallback`` is ``True``.
        """
        agents = directory / "AGENTS.md"
        claude = directory / "CLAUDE.md"
        agents_exists = agents.exists()
        claude_exists = claude.exists()

        if agents_exists:
            return agents, False, (claude if claude_exists else None)
        if claude_exists:
            return claude, True, None
        return None

    def _nested_sources(self, workspace: Path) -> list[SteeringSource]:
        """Find nested steering files below the workspace root.

        Skips ``_SKIP_DIRS`` (VCS metadata, runtime state, tool
        convention directories) and does not follow directory symlinks.
        Returns sources ordered by depth (shallowest first), then by
        subtree path for determinism.
        """
        found: list[tuple[int, str, Path, bool, Path | None]] = []
        for root, dirs, _files in os.walk(workspace, followlinks=False):
            # Prune skip dirs in place so os.walk does not descend into them.
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
            root_path = Path(root)
            if root_path == workspace:
                continue  # workspace root is handled as its own level
            result = self._agents_or_claude(root_path)
            if result is None:
                continue
            chosen_path, is_fallback, shadowed = result
            rel = root_path.relative_to(workspace)
            subtree = rel.as_posix() if rel.parts else "."
            found.append((len(rel.parts), subtree, chosen_path, is_fallback, shadowed))

        found.sort(key=lambda item: (item[0], item[1]))
        return [
            SteeringSource(
                path=path,
                precedence=Precedence.NESTED,
                subtree=subtree,
                is_fallback=is_fallback,
                shadowed_path=shadowed,
            )
            for _, subtree, path, is_fallback, shadowed in found
        ]
