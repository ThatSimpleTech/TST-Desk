"""Slash-command discovery (TD-4501).

Commands are short markdown files the composer lists behind ``/`` and
inserts into a draft on selection.  They are deliberately *not* steering
(TD-501): nothing here ever touches the prompt cache prefix — a body only
enters the conversation when the user actually invokes it.

Sources, per the story:

* ``<workspace>/.tst/commands/*.md``
* ``~/.tstdesk/commands/*.md``  (user-global; wins on name collision)
* ``.claude/commands/*.md``     (fallback when a level's own dir is empty)

The ``home`` parameter is injected rather than calling ``Path.home()``
inlined, mirroring the steering/memory loaders, so tests can stage a
fake HOME tree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from tstd.context.frontmatter import parse_frontmatter

log = logging.getLogger(__name__)

#: Workspace-local command directory.
WORKSPACE_COMMANDS_DIR = Path(".tst") / "commands"
#: User-global command directory under the injected home.
USER_COMMANDS_DIR = Path(".tstdesk") / "commands"
#: Claude-compatible fallback tried when a level's own directory is empty.
CLAUDE_COMMANDS_DIR = Path(".claude") / "commands"

#: Over this many lines a command still loads, but says so — same soft
#: warning posture as steering sources (assembler.py), never a refusal.
COMMAND_SOFT_LINE_LIMIT = 200


@dataclass(frozen=True)
class Command:
    """One discovered slash command, body read from disk.

    ``source`` is one of ``workspace``, ``user``, ``workspace_fallback``,
    ``user_fallback`` — the last two mark ``.claude/commands`` reads.
    """

    name: str
    source: str
    path: Path
    body: str
    description: str | None = None
    line_count: int = 0


def _read_command(path: Path, root: Path, source: str) -> Command | None:
    """Read one candidate file, or None on any condition worth skipping."""
    try:
        resolved = path.resolve()
    except OSError as exc:
        log.warning(
            "command file unreadable, skipped",
            extra={"extra_fields": {"path": str(path), "error": str(exc)}},
        )
        return None
    # A symlinked command file may not point outside its directory —
    # same containment rule the memory loader applies to candidates.
    # The scan is one level deep, so an exact parent match is the rule.
    if not resolved.is_file() or resolved.parent != root:
        log.warning(
            "command path escaped its commands directory, skipped",
            extra={"extra_fields": {"path": str(path), "resolved": str(resolved)}},
        )
        return None
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "command file unreadable, skipped",
            extra={"extra_fields": {"path": str(resolved), "error": str(exc)}},
        )
        return None
    except UnicodeDecodeError:
        log.warning(
            "command file is not valid UTF-8, skipped",
            extra={"extra_fields": {"path": str(resolved)}},
        )
        return None

    metadata, body = parse_frontmatter(text)
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        description = None
    line_count = body.count("\n") + (0 if body.endswith("\n") or not body else 1)
    if line_count > COMMAND_SOFT_LINE_LIMIT:
        log.warning(
            "command file exceeds the soft line limit",
            extra={"extra_fields": {"path": str(resolved), "lines": line_count}},
        )
    return Command(
        name=path.stem,
        source=source,
        path=resolved,
        body=body,
        description=description,
        line_count=line_count,
    )


def _scan_dir(root: Path, source: str) -> list[Command]:
    """Read every ``*.md`` directly inside *root*; missing dir is fine."""
    if not root.is_dir():
        return []
    found: list[Command] = []
    for candidate in sorted(root.glob("*.md")):
        command = _read_command(candidate, root.resolve(), source)
        if command is not None:
            found.append(command)
    return found


def discover_commands(workspace: Path, home: Path | None = None) -> list[Command]:
    """List every command visible to *workspace*, sorted by name.

    Per level, ``.claude/commands`` is consulted only when that level's
    own directory holds no commands.  When a name exists at both levels,
    the user-global definition wins (TD-4501) — the workspace copy is
    shadowed, not merged.
    """
    if home is None:
        home = Path.home()

    by_name: dict[str, Command] = {}

    workspace_own = _scan_dir(workspace / WORKSPACE_COMMANDS_DIR, "workspace")
    if workspace_own:
        for command in workspace_own:
            by_name[command.name] = command
    else:
        for command in _scan_dir(workspace / CLAUDE_COMMANDS_DIR, "workspace_fallback"):
            by_name[command.name] = command

    user_own = _scan_dir(home / USER_COMMANDS_DIR, "user")
    if user_own:
        for command in user_own:  # user-global wins on collision
            by_name[command.name] = command
    else:
        for command in _scan_dir(home / CLAUDE_COMMANDS_DIR, "user_fallback"):
            by_name[command.name] = command

    return sorted(by_name.values(), key=lambda c: c.name)
