"""Slash-command discovery and expansion (TD-4501).

Commands are human-written markdown the composer offers after a leading
``/``. They are steering's sibling, not its child: like steering they are
read-only to the agent, but unlike steering they never enter the cache
prefix — a command body is spliced into the one turn that invokes it,
so editing a command file cannot re-bill the whole prefix.

Two trees, with the inverse of steering's precedence: the user-global
``~/.tstdesk/commands/*.md`` wins a name over the workspace's
``.tst/commands/*.md``, because a personal override should beat a
checked-in default the same way a local config beats an installed one.
When a tree has no commands of our own it falls back to the Claude Code
convention — ``.claude/commands/`` beside it — flagged so the UI can say
where a command came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..logging import get_logger

log = get_logger("tstd.commands")

# A command name is a filename stem; these are the stems the composer can
# spell after a ``/`` without quoting tricks. Anything else is not hidden,
# just not offered or invokable.
_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n.*?\n(?:---|\.\.\.)[ \t]*\n?", re.DOTALL)


@dataclass(frozen=True)
class CommandFile:
    """One discovered command file."""

    name: str
    path: Path
    source: Literal["workspace", "user"]
    # True when this file serves only because the tree had no commands of
    # our own — the .claude/commands fallback (TD-4501).
    fallback: bool = False

    @property
    def display_path(self) -> str:
        """POSIX-separated path for listings, ~-shortened for user files."""
        text = self.path.as_posix()
        home = Path.home()
        if self.source == "user":
            home_text = home.as_posix()
            if text.startswith(home_text):
                text = "~" + text[len(home_text) :]
        return text


def global_commands_dir(home: str | Path) -> Path:
    """``~/.tstdesk/commands`` — the user-global tree."""
    return Path(home) / ".tstdesk" / "commands"


def _discover_tree(root: Path) -> dict[str, Path]:
    """Stem → path for one commands directory, containment-checked.

    A symlink that escapes the directory is skipped rather than followed:
    the trees are human-written, and a link out is either a mistake or a
    way to feed the model something the tree's owner did not write.
    """
    try:
        if not root.is_dir():
            return {}
        root = root.resolve()
    except OSError as exc:
        log.warning(
            "commands directory unreadable, skipped",
            extra={"extra_fields": {"path": str(root), "error": str(exc)}},
        )
        return {}
    found: dict[str, Path] = {}
    for path in sorted(root.glob("*.md")):
        name = path.stem
        if _NAME_RE.fullmatch(name) is None:
            continue
        try:
            resolved = path.resolve()
        except OSError as exc:
            log.warning(
                "command file unreadable, skipped",
                extra={"extra_fields": {"path": str(path), "error": str(exc)}},
            )
            continue
        try:
            resolved.relative_to(root)
        except ValueError:
            log.warning(
                "command path escaped its commands directory, skipped",
                extra={"extra_fields": {"path": str(path), "resolved": str(resolved)}},
            )
            continue
        found[name] = resolved
    return found


def discover_commands(
    workspace: str | Path, home_dir: str | Path | None = None
) -> list[CommandFile]:
    """List the commands available for *workspace*, best-name-first.

    User-global wins on a shared name (the inverse of steering, where the
    workspace wins). Each tree falls back to its Claude Code counterpart
    only when it holds no commands of ours.
    """
    home = Path(home_dir).expanduser() if home_dir is not None else Path.home()
    ours_ws = _discover_tree(Path(workspace) / ".tst" / "commands")
    fallback_ws = {} if ours_ws else _discover_tree(Path(workspace) / ".claude" / "commands")
    ours_user = _discover_tree(global_commands_dir(home))
    fallback_user = {} if ours_user else _discover_tree(home / ".claude" / "commands")

    merged: dict[str, CommandFile] = {}
    # Lower precedence first so a later insert replaces it on collision:
    # workspace fallback < workspace < user fallback < user.
    layers: tuple[tuple[dict[str, Path], Literal["workspace", "user"], bool], ...] = (
        (fallback_ws, "workspace", True),
        (ours_ws, "workspace", False),
        (fallback_user, "user", True),
        (ours_user, "user", False),
    )
    for names, source, fallback in layers:
        for name in sorted(names):
            merged[name] = CommandFile(
                name=name, path=names[name], source=source, fallback=fallback
            )
    return [merged[name] for name in sorted(merged)]


def read_command_body(path: Path) -> str:
    """Read one command file, stripping any YAML frontmatter.

    Frontmatter is metadata for the loader, not prose for the model
    (skills formalize it in TD-4502); splicing it would read noise.
    """
    text = path.read_text(encoding="utf-8")
    return _FRONTMATTER_RE.sub("", text, count=1)


def expand_command(command: CommandFile, args: str | None) -> str:
    """Render the message the model reads when *command* is invoked.

    Plain delimiter lines, per ``render_user_content``'s reasoning: a
    fence would need escaping the moment the body held one. The original
    spelling stays at the top so the transcript shows what was typed.
    """
    argument_line = args.strip() if args else ""
    parts = [
        f"--- slash command: /{command.name} ({command.display_path}) ---",
        read_command_body(command.path).strip(),
        "--- end of slash command ---",
    ]
    if argument_line:
        parts.append(f"Command arguments from the user:\n{argument_line}")
    return "\n\n".join(parts)
