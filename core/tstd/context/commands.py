"""Slash-command discovery (TD-4501).

Human-authored markdown under ``.tst/commands/`` and
``~/.tstdesk/commands/``.  Not steering: the assembler never reads these
trees.  User-global wins on the same stem.  When both of *our* trees have
no ``.md`` files, fall back to ``.claude/commands/`` and
``~/.claude/commands/``.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .frontmatter import parse_frontmatter

CommandSource = Literal["workspace", "user", "claude_workspace", "claude_user"]

# Insert payload cap. Recorded in DECISIONS.md (TD-4501).
COMMAND_BODY_CAP = 32 * 1024

_COMMAND_STEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class SlashCommand:
    """One discovered slash command.

    ``name`` is the slash stem (``review.md`` → ``review``).  ``body`` is
    the insert text after frontmatter is stripped.  When the body exceeds
    :data:`COMMAND_BODY_CAP`, ``body`` is empty and ``too_large`` is true.
    """

    name: str
    description: str
    source: CommandSource
    body: str
    path: Path
    too_large: bool = False


class CommandDiscoverer:
    """Finds slash-command files for a workspace.

    Args:
        home_dir: Override the user home directory (test seam).
            Defaults to the real user home.
    """

    def __init__(self, home_dir: str | Path | None = None) -> None:
        self._home = Path(home_dir).expanduser() if home_dir is not None else Path.home()

    @property
    def home_dir(self) -> Path:
        """The home directory used for user-global command trees."""
        return self._home

    def discover(self, workspace_path: str | Path) -> list[SlashCommand]:
        """Discover commands for *workspace_path*, user-global winning on stem.

        Missing trees are not errors.  Invalid stems (spaces, path
        separators) are skipped.  Non-``*.md`` files are ignored.
        """
        workspace = Path(workspace_path).expanduser().resolve()
        ours_ws = self._md_files(workspace / ".tst" / "commands")
        ours_user = self._md_files(self._home / ".tstdesk" / "commands")
        if ours_ws or ours_user:
            return self._merge(
                self._parse_tree(ours_ws, "workspace"),
                self._parse_tree(ours_user, "user"),
            )
        claude_ws = self._md_files(workspace / ".claude" / "commands")
        claude_user = self._md_files(self._home / ".claude" / "commands")
        return self._merge(
            self._parse_tree(claude_ws, "claude_workspace"),
            self._parse_tree(claude_user, "claude_user"),
        )

    @staticmethod
    def _md_files(directory: Path) -> list[Path]:
        if not directory.is_dir():
            return []
        return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix == ".md")

    def _parse_tree(self, files: list[Path], source: CommandSource) -> dict[str, SlashCommand]:
        found: dict[str, SlashCommand] = {}
        for path in files:
            parsed = self._parse_file(path, source)
            if parsed is not None:
                found[parsed.name] = parsed
        return found

    def _parse_file(self, path: Path, source: CommandSource) -> SlashCommand | None:
        stem = path.stem
        if not _COMMAND_STEM.match(stem):
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        metadata, body = parse_frontmatter(raw)
        description = ""
        raw_desc = metadata.get("description")
        if isinstance(raw_desc, str):
            description = raw_desc.strip()
        too_large = len(body.encode("utf-8")) > COMMAND_BODY_CAP
        return SlashCommand(
            name=stem,
            description=description,
            source=source,
            body="" if too_large else body,
            path=path,
            too_large=too_large,
        )

    @staticmethod
    def _merge(
        lower: dict[str, SlashCommand],
        higher: dict[str, SlashCommand],
    ) -> list[SlashCommand]:
        """*higher* wins on the same stem. Result is sorted by name."""
        merged = {**lower, **higher}
        return [merged[name] for name in sorted(merged)]


def list_workspace_commands(
    workspace: str | Path,
    home_dir: str | Path | None = None,
) -> list[SlashCommand]:
    """Synchronous discovery — wrap in ``asyncio.to_thread`` on the I/O path."""
    return CommandDiscoverer(home_dir=home_dir).discover(workspace)


async def list_workspace_commands_async(
    workspace: str | Path,
    home_dir: str | Path | None = None,
) -> list[SlashCommand]:
    """Discover commands off the event loop."""
    return await asyncio.to_thread(list_workspace_commands, workspace, home_dir)
