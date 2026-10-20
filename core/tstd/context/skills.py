"""Skill catalog and loading (TD-4502).

Discovery lives in :mod:`tstd.context.skill_discovery`.  Not steering:
the assembler never reads skill trees into the cache prefix.  The brain
sees a name+description catalog; bodies attach only after ``load_skill``
or a slash invoke.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from pathlib import Path

from .skill_discovery import (
    SKILL_BODY_TOKEN_CAP,
    Skill,
    SkillDiscoverer,
    SkillSource,
    list_workspace_skills,
    list_workspace_skills_async,
    session_claude_global_fallback,
    warn_once,
)

__all__ = [
    "SKILL_BODY_TOKEN_CAP",
    "SKILL_LOAD_MARKER",
    "Skill",
    "SkillDiscoverer",
    "SkillSource",
    "apply_slash_skill",
    "handle_load_skill",
    "list_workspace_skills",
    "list_workspace_skills_async",
    "register_skill_handlers",
    "register_skill_tools",
    "render_loaded_skill",
    "render_loaded_skills",
    "render_skill_catalog",
    "skills_doctor_detail",
    "slash_skill_name",
    "try_load_skill",
]

_SLASH_SKILL = re.compile(r"^\s*/([A-Za-z0-9][A-Za-z0-9._-]*)(?:\s|$)")
_LOAD_MARKER = re.compile(r"Load skill `([^`]+)`")

SKILL_LOAD_MARKER = "Load skill `{name}`."


def render_skill_catalog(skills: Sequence[Skill]) -> str:
    """Short name+description list for the brain prompt. Not bodies.

    A skill over :data:`SKILL_BODY_TOKEN_CAP` is left out: ``load_skill``
    would refuse it every time, and each try costs the model a turn.  A
    ``user_only`` skill is left out too: only the user's ``/name`` may
    load it (``disable-model-invocation``, TD-5107).
    """
    loadable = [
        skill for skill in skills if skill.tokens <= SKILL_BODY_TOKEN_CAP and not skill.user_only
    ]
    for skill in skills:
        if skill.tokens > SKILL_BODY_TOKEN_CAP:
            warn_once(
                "skill left out of the catalog: over the body cap",
                skill.path,
                tokens=skill.tokens,
                cap=SKILL_BODY_TOKEN_CAP,
            )
    if not loadable:
        return ""
    lines = [
        "## Skills",
        "Call `load_skill` with a name to load the full instructions. "
        "Slash `/name` loads a skill that is not also a command.",
    ]
    for skill in loadable:
        line = f"- `{skill.name}`: {skill.description}"
        if skill.when_to_use != skill.description:
            # A Claude Code skill has no `whenToUse`; its description already
            # says when, so it is not repeated (TD-5011).
            line += f" (when to use: {skill.when_to_use})"
        if skill.compatibility:
            # Agent Skills' environment needs, so the model can tell a skill
            # it cannot run here (TD-5107).
            line += f" (compatibility: {skill.compatibility})"
        lines.append(line)
    return "\n".join(lines)


def skills_doctor_detail(skills: Sequence[Skill]) -> str:
    """Doctor's ``skills`` row: the count, then each skill's ignored keys.

    Names only, never paths: the row is copied into a pasted report
    (TD-1104).  A skill with ignored keys loaded; the row says which keys
    it carried that TST Desk did not act on (TD-5107).
    """
    if not skills:
        return "no skills found"
    detail = f"{len(skills)} skill(s) found"
    flagged = [f"{s.name} ({', '.join(s.ignored_keys)})" for s in skills if s.ignored_keys]
    if flagged:
        detail += "; frontmatter keys ignored: " + "; ".join(flagged)
    return detail


def render_loaded_skill(skill: Skill) -> str:
    """Full skill body for a loaded skill, placed after the catalog.

    The directory line lets a body that runs ``python3 chapters.py`` or
    reads ``references/guide.md`` find those files: the shell starts in
    the workspace, not in the skill's folder.
    """
    where = f"Skill directory: {skill.directory} (relative paths in this skill are relative to it)."
    return f"## Skill: {skill.name}\n{where}\n{skill.body}"


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
    claude_global_fallback: bool | None = None,
    by_user: bool = False,
) -> str:
    """Attach *name* to ``session.loaded_skills`` or return a refusal.

    Loading the same name twice is a no-op.  An over-budget body is
    refused, not truncated.  Unknown names are refused.  A ``user_only``
    skill is refused unless *by_user* (the user's ``/name``, TD-5107).
    A None *claude_global_fallback* reads the session's config.
    """
    workspace = getattr(session, "workspace_path", None)
    if not isinstance(workspace, str):
        return "Error: session has no workspace_path; cannot load a skill"
    loaded = getattr(session, "loaded_skills", None)
    if not isinstance(loaded, list):
        return "Error: session has no loaded_skills list"
    if name in loaded:
        return f"Skill `{name}` is already loaded."
    if claude_global_fallback is None:
        claude_global_fallback = session_claude_global_fallback(session)
    found = list_workspace_skills(
        workspace, home_dir, claude_global_fallback=claude_global_fallback
    )
    skill = {s.name: s for s in found}.get(name)
    if skill is None:
        return f"Error: unknown skill `{name}`"
    if skill.user_only and not by_user:
        return (
            f"Error: skill `{name}` sets disable-model-invocation; "
            f"only the user can load it, with /{name}."
        )
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
    claude_global_fallback: bool | None = None,
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
    if claude_global_fallback is None:
        claude_global_fallback = session_claude_global_fallback(session)
    found = list_workspace_skills(
        workspace, home_dir, claude_global_fallback=claude_global_fallback
    )
    if name not in {s.name for s in found}:
        return None
    return try_load_skill(
        session,
        name,
        home_dir=home_dir,
        claude_global_fallback=claude_global_fallback,
        by_user=True,
    )


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
