"""Skill discovery and loading (TD-4502).

Human-authored ``SKILL.md`` files under ``.tst/skills/<name>/`` and
``~/.tstdesk/skills/<name>/``.  Not steering: the assembler never reads
these trees into the cache prefix.  The brain sees a name+description
catalog; bodies attach only after ``load_skill`` or a slash invoke.

User-global wins on the same ``<name>``.  When both of *our* trees have
no ``SKILL.md``, fall back to ``.claude/skills/`` and ``~/.claude/skills/``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..logging import get_logger
from .frontmatter import parse_frontmatter
from .tokens import heuristic_count

log = get_logger("tstd.skills")

SkillSource = Literal["workspace", "user", "claude_workspace", "claude_user"]

# Fixed body cap. Recorded in DECISIONS.md (TD-4502).
SKILL_BODY_TOKEN_CAP = 4000

_ALLOWED_FRONTMATTER = frozenset({"description", "whenToUse"})
_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SLASH_SKILL = re.compile(r"^\s*/([A-Za-z0-9][A-Za-z0-9._-]*)(?:\s|$)")
_LOAD_MARKER = re.compile(r"Load skill `([^`]+)`")

SKILL_LOAD_MARKER = "Load skill `{name}`."


@dataclass(frozen=True)
class Skill:
    """One discovered skill.

    ``body`` is the markdown after frontmatter.  ``tokens`` is the
    heuristic count of that body (the over-budget check uses the same
    figure).
    """

    name: str
    description: str
    when_to_use: str
    source: SkillSource
    body: str
    path: Path
    tokens: int


class SkillDiscoverer:
    """Finds ``SKILL.md`` files for a workspace.

    Args:
        home_dir: Override the user home directory (test seam).
            Defaults to the real user home.
    """

    def __init__(self, home_dir: str | Path | None = None) -> None:
        self._home = Path(home_dir).expanduser() if home_dir is not None else Path.home()

    @property
    def home_dir(self) -> Path:
        """The home directory used for user-global skill trees."""
        return self._home

    def discover(self, workspace_path: str | Path) -> list[Skill]:
        """Discover skills for *workspace_path*, user-global winning on name.

        Missing trees are not errors.  Invalid directory names are
        skipped.  Extra or missing frontmatter keys skip that skill.
        """
        workspace = Path(workspace_path).expanduser().resolve()
        ours_ws = self._skill_files(workspace / ".tst" / "skills")
        ours_user = self._skill_files(self._home / ".tstdesk" / "skills")
        if ours_ws or ours_user:
            return self._merge(
                self._parse_tree(ours_ws, "workspace"),
                self._parse_tree(ours_user, "user"),
            )
        claude_ws = self._skill_files(workspace / ".claude" / "skills")
        claude_user = self._skill_files(self._home / ".claude" / "skills")
        return self._merge(
            self._parse_tree(claude_ws, "claude_workspace"),
            self._parse_tree(claude_user, "claude_user"),
        )

    @staticmethod
    def _skill_files(directory: Path) -> list[Path]:
        if not directory.is_dir():
            return []
        found: list[Path] = []
        for child in sorted(directory.iterdir()):
            if not child.is_dir():
                continue
            skill = child / "SKILL.md"
            if skill.is_file():
                found.append(skill)
        return found

    def _parse_tree(self, files: list[Path], source: SkillSource) -> dict[str, Skill]:
        found: dict[str, Skill] = {}
        for path in files:
            parsed = self._parse_file(path, source)
            if parsed is not None:
                found[parsed.name] = parsed
        return found

    def _parse_file(self, path: Path, source: SkillSource) -> Skill | None:
        name = path.parent.name
        if not _SKILL_NAME.match(name):
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        metadata, body = parse_frontmatter(raw)
        extra = set(metadata) - _ALLOWED_FRONTMATTER
        if extra:
            log.warning(
                "skill skipped: extra frontmatter keys",
                extra={"extra_fields": {"path": str(path), "keys": sorted(extra)}},
            )
            return None
        raw_desc = metadata.get("description")
        raw_when = metadata.get("whenToUse")
        if not isinstance(raw_desc, str) or not raw_desc.strip():
            return None
        if not isinstance(raw_when, str) or not raw_when.strip():
            return None
        return Skill(
            name=name,
            description=raw_desc.strip(),
            when_to_use=raw_when.strip(),
            source=source,
            body=body,
            path=path,
            tokens=heuristic_count(body).count,
        )

    @staticmethod
    def _merge(lower: dict[str, Skill], higher: dict[str, Skill]) -> list[Skill]:
        """*higher* wins on the same name. Result is sorted by name."""
        merged = {**lower, **higher}
        return [merged[name] for name in sorted(merged)]


def list_workspace_skills(
    workspace: str | Path,
    home_dir: str | Path | None = None,
) -> list[Skill]:
    """Synchronous discovery — wrap in ``asyncio.to_thread`` on the I/O path."""
    return SkillDiscoverer(home_dir=home_dir).discover(workspace)


async def list_workspace_skills_async(
    workspace: str | Path,
    home_dir: str | Path | None = None,
) -> list[Skill]:
    """Discover skills off the event loop."""
    return await asyncio.to_thread(list_workspace_skills, workspace, home_dir)


def render_skill_catalog(skills: Sequence[Skill]) -> str:
    """Short name+description list for the brain prompt. Not bodies."""
    if not skills:
        return ""
    lines = [
        "## Skills",
        "Call `load_skill` with a name to load the full instructions. "
        "Slash `/name` loads a skill that is not also a command.",
    ]
    for skill in skills:
        lines.append(f"- `{skill.name}`: {skill.description} (when to use: {skill.when_to_use})")
    return "\n".join(lines)


def render_loaded_skill(skill: Skill) -> str:
    """Full skill body for a loaded skill, placed after the catalog."""
    return f"## Skill: {skill.name}\n{skill.body}"


def render_loaded_skills(skills: Sequence[Skill], loaded_names: Sequence[str]) -> str:
    """Bodies of loaded skills, in load order. Missing names are skipped."""
    by_name = {s.name: s for s in skills}
    parts = [render_loaded_skill(by_name[name]) for name in loaded_names if name in by_name]
    return "\n\n".join(parts)


def try_load_skill(
    session: object,
    name: str,
    *,
    home_dir: str | Path | None = None,
) -> str:
    """Attach *name* to ``session.loaded_skills`` or return a refusal.

    Loading the same name twice is a no-op.  An over-budget body is
    refused, not truncated.  Unknown names are refused.
    """
    workspace = getattr(session, "workspace_path", None)
    if not isinstance(workspace, str):
        return "Error: session has no workspace_path; cannot load a skill"
    loaded = getattr(session, "loaded_skills", None)
    if not isinstance(loaded, list):
        return "Error: session has no loaded_skills list"
    if name in loaded:
        return f"Skill `{name}` is already loaded."
    skills = {s.name: s for s in list_workspace_skills(workspace, home_dir)}
    skill = skills.get(name)
    if skill is None:
        return f"Error: unknown skill `{name}`"
    if skill.tokens > SKILL_BODY_TOKEN_CAP:
        return (
            f"Error: skill `{name}` is over budget "
            f"({skill.tokens} tokens > {SKILL_BODY_TOKEN_CAP}). "
            "Refused, not truncated."
        )
    loaded.append(name)
    return f"Loaded skill `{name}` ({skill.tokens} tokens)."


def slash_skill_name(text: str) -> str | None:
    """Skill name from a leading ``/name`` or a load-skill marker, if any."""
    match = _SLASH_SKILL.match(text)
    if match is not None:
        return match.group(1)
    marker = _LOAD_MARKER.search(text)
    if marker is not None:
        return marker.group(1)
    return None


def apply_slash_skill(
    session: object,
    text: str,
    *,
    home_dir: str | Path | None = None,
) -> str | None:
    """Load a skill if *text* slashes a skill that is not also a command.

    Commands from TD-4501 win on the same stem.  Returns the load
    result, or ``None`` when this is not a skill invoke.
    """
    from .commands import list_workspace_commands

    name = slash_skill_name(text)
    if name is None:
        return None
    workspace = getattr(session, "workspace_path", None)
    if not isinstance(workspace, str):
        return None
    command_names = {c.name for c in list_workspace_commands(workspace, home_dir)}
    if name in command_names:
        return None
    skills = {s.name for s in list_workspace_skills(workspace, home_dir)}
    if name not in skills:
        return None
    return try_load_skill(session, name, home_dir=home_dir)


async def handle_load_skill(session: object, name: str, tool_call_id: str = "") -> str:
    """``load_skill`` handler — attach a named skill to the session."""
    return await asyncio.to_thread(try_load_skill, session, name)


def register_skill_tools(registry: object) -> None:
    """Register the builtin ``load_skill`` tool. Call from ``create_registry``."""
    from ..tools.registry import Tool, ToolRegistry

    if not isinstance(registry, ToolRegistry):
        raise TypeError("register_skill_tools expects a ToolRegistry")
    registry.register(
        Tool(
            name="load_skill",
            description=(
                "Load a named skill's full instructions into this session. "
                "Use a name from the Skills catalog. Loading the same skill "
                "twice is a no-op. An over-budget skill is refused, not truncated."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name from the catalog",
                    },
                },
                "required": ["name"],
            },
            side_effect_class="auto",
            parallel_safe=True,
        )
    )


def register_skill_handlers(dispatcher: object) -> None:
    """Register the ``load_skill`` handler. Call from ``register_builtin_handlers``."""
    from ..tools.dispatch import ToolDispatcher

    if not isinstance(dispatcher, ToolDispatcher):
        raise TypeError("register_skill_handlers expects a ToolDispatcher")
    dispatcher.register_handler("load_skill", handle_load_skill)
