"""Skill discovery (TD-4502).

A skill is a directory holding one ``SKILL.md`` — reference material a
human writes for the agent, complementary to steering (E45): steering is
always in context, a skill costs nothing until invoked.  The brain sees
a name + description catalog; bodies load on demand via the
``load_skill`` tool or a ``/name`` slash invocation, both of which land
in the conversation tail — after the cache prefix, never inside it.

Sources, mirroring slash commands:

* ``<workspace>/.tst/skills/<name>/SKILL.md``
* ``~/.tstdesk/skills/<name>/SKILL.md``  (user-global; wins on collision)
* ``.claude/skills/<name>/SKILL.md``     (fallback when a level's own dir
  is empty)

The ``home`` parameter is injected rather than calling ``Path.home()``
inlined, mirroring the commands/memory loaders, so tests can stage a
fake HOME tree.

Writes to ``**/SKILL.md`` are Class C via the classifier's steering
basenames — the loader deliberately does no classifying of its own.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tstd.context.frontmatter import parse_frontmatter
from tstd.context.tokens import heuristic_count
from tstd.protocol import LoadedSkillEntry

log = logging.getLogger(__name__)

#: Workspace-local skills root; each child directory is one skill.
WORKSPACE_SKILLS_DIR = Path(".tst") / "skills"
#: User-global skills root under the injected home.
USER_SKILLS_DIR = Path(".tstdesk") / "skills"
#: Claude-compatible fallback tried when a level's own root is empty.
CLAUDE_SKILLS_DIR = Path(".claude") / "skills"

#: The only file name a skill dir may contribute to discovery.
SKILL_FILE = "SKILL.md"

#: Hard budget for a loadable body, in heuristic tokens — the same
#: default as the memory and project-context budgets.  An over-budget
#: skill is *refused*, not truncated (TD-4502): silently clipping human
#: instructions is how an agent follows half a procedure and calls it
#: done.
SKILL_MAX_TOKENS = 2000

#: Over this many lines a skill still loads, but says so — same soft
#: warning posture as steering sources and commands, never a refusal.
SKILL_SOFT_LINE_LIMIT = 200


@dataclass(frozen=True)
class Skill:
    """One discovered skill.

    ``source`` is one of ``workspace``, ``user``, ``workspace_fallback``,
    ``user_fallback`` — the last two mark ``.claude/skills`` reads.
    ``body`` stays daemon-side: the wire carries the catalog fields
    only, and the body rides ``load_skill`` or a slash invocation.
    """

    name: str
    source: str
    path: Path
    body: str
    description: str | None = None
    when_to_use: str | None = None
    line_count: int = 0


def _read_skill(path: Path, source: str) -> Skill | None:
    """Read one candidate SKILL.md, or None on any condition worth skipping."""
    try:
        resolved = path.resolve()
    except OSError as exc:
        log.warning(
            "skill file unreadable, skipped",
            extra={"extra_fields": {"path": str(path), "error": str(exc)}},
        )
        return None
    if not resolved.is_file():
        log.warning(
            "skill path is not a file, skipped",
            extra={"extra_fields": {"path": str(resolved)}},
        )
        return None
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "skill file unreadable, skipped",
            extra={"extra_fields": {"path": str(resolved), "error": str(exc)}},
        )
        return None
    except UnicodeDecodeError:
        log.warning(
            "skill file is not valid UTF-8, skipped",
            extra={"extra_fields": {"path": str(resolved)}},
        )
        return None

    metadata, body = parse_frontmatter(text)
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        description = None
    when_to_use = metadata.get("whenToUse")
    if not isinstance(when_to_use, str) or not when_to_use.strip():
        when_to_use = None
    line_count = body.count("\n") + (0 if body.endswith("\n") or not body else 1)
    if line_count > SKILL_SOFT_LINE_LIMIT:
        log.warning(
            "skill file exceeds the soft line limit",
            extra={"extra_fields": {"path": str(resolved), "lines": line_count}},
        )
    return Skill(
        name=resolved.parent.name,
        source=source,
        path=resolved,
        body=body,
        description=description,
        when_to_use=when_to_use,
        line_count=line_count,
    )


def _scan_dir(root: Path, source: str) -> list[Skill]:
    """Read every ``<dir>/SKILL.md`` one level under *root*; missing is fine."""
    if not root.is_dir():
        return []
    if root.is_symlink():
        # Fail closed on a symlinked skills root (TD-4502 review): resolving
        # first would move the wall to wherever the link points, and every
        # candidate would then pass containment by construction. A checkout
        # can carry such a link, so repo content could aim the scanner at
        # any directory the user can read.
        log.warning(
            "skills root is a symlink, skipped",
            extra={"extra_fields": {"path": str(root)}},
        )
        return []
    try:
        resolved_root = root.resolve()
    except OSError as exc:
        log.warning(
            "skills directory unreadable, skipped",
            extra={"extra_fields": {"path": str(root), "error": str(exc)}},
        )
        return []
    found: list[Skill] = []
    for candidate in sorted(root.glob(f"*/{SKILL_FILE}")):
        skill = _read_skill(candidate, source)
        if skill is None:
            continue
        # A symlinked skill dir may not point outside its skills root —
        # the scan is one level deep, so the body's parent must be a
        # direct child of the resolved root.
        if skill.path.parent.parent != resolved_root:
            log.warning(
                "skill directory escaped its skills root, skipped",
                extra={
                    "extra_fields": {
                        "path": str(candidate),
                        "resolved": str(skill.path),
                    }
                },
            )
            continue
        found.append(skill)
    return found


def discover_skills(workspace: Path, home: Path | None = None) -> list[Skill]:
    """List every skill visible to *workspace*, sorted by name.

    Per level, ``.claude/skills`` is consulted only when that level's
    own root holds no skills.  When a name exists at both levels, the
    user-global definition wins (mirroring commands, TD-4501) — the
    workspace copy is shadowed, not merged.
    """
    if home is None:
        home = Path.home()

    by_name: dict[str, Skill] = {}

    workspace_own = _scan_dir(workspace / WORKSPACE_SKILLS_DIR, "workspace")
    if workspace_own:
        for skill in workspace_own:
            by_name[skill.name] = skill
    else:
        for skill in _scan_dir(workspace / CLAUDE_SKILLS_DIR, "workspace_fallback"):
            by_name[skill.name] = skill

    user_own = _scan_dir(home / USER_SKILLS_DIR, "user")
    if user_own:
        for skill in user_own:  # user-global wins on collision
            by_name[skill.name] = skill
    else:
        for skill in _scan_dir(home / CLAUDE_SKILLS_DIR, "user_fallback"):
            by_name[skill.name] = skill

    return sorted(by_name.values(), key=lambda s: s.name)


def find_skill(skills: Sequence[Skill], name: str) -> Skill | None:
    """Exact-name lookup against an already-discovered listing."""
    for skill in skills:
        if skill.name == name:
            return skill
    return None


def skill_budget_refusal(skill: Skill) -> str | None:
    """Why *skill* must not load, or None when it fits the budget."""
    tokens = heuristic_count(skill.body).count
    if tokens <= SKILL_MAX_TOKENS:
        return None
    return (
        f"skill '{skill.name}' is over the {SKILL_MAX_TOKENS}-token budget "
        f"({tokens} tokens) — refused, not truncated. Split it into smaller "
        "skills or move detail into files the skill can point at."
    )


def record_load(entries: list[LoadedSkillEntry], skill: Skill) -> None:
    """Note a load for the inspector, one entry per name however many
    times the body enters context. Shared by ``load_skill`` and the
    loop's slash expansion so both report identically."""
    entry = LoadedSkillEntry(
        name=skill.name,
        source=skill.source,
        tokens=heuristic_count(skill.body).count,
    )
    entries[:] = [e for e in entries if e.name != entry.name] + [entry]


def render_catalog(skills: Sequence[Skill]) -> str | None:
    """Render the brain's name + description catalog, or None when empty.

    This block sits after the cache prefix (brain tier blocks follow
    steering), so it may change per turn without touching prefix bytes —
    but it still lists only what discovery found, never bodies.
    """
    if not skills:
        return None
    lines = [
        "<!-- skills: human-written playbooks. Their full text is NOT here;",
        "     call load_skill with a name to load one when the task needs it. -->",
        "### Available skills",
    ]
    for skill in skills:
        parts = [f"- {skill.name}:"]
        if skill.description:
            parts.append(skill.description.strip())
        if skill.when_to_use:
            parts.append(f"Use when: {skill.when_to_use.strip()}.")
        lines.append(" ".join(parts))
    return "\n".join(lines)
