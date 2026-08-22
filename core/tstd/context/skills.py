"""SKILL.md discovery and loading (TD-4502).

A skill is progressive disclosure for the brain tier: the prompt carries
only a name+description catalog, and the full body arrives later — via
the ``load_skill`` tool or an invoked ``/name`` — never inside the cache
prefix. Editing a skill cannot re-bill the prefix, the same property
commands got from splicing.

Layout mirrors Claude Code's convention: ``<skills-dir>/<name>/SKILL.md``
with YAML frontmatter holding at most ``description`` and ``whenToUse``.
Two trees, user-global winning a name — same precedence as commands,
because a personal override beats a checked-in default. When a tree has
no skills of our own it falls back to its ``.claude/skills`` twin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from ..logging import get_logger

log = get_logger("tstd.skills")

# Budget floor used only when no model config is wired (unit tests).
# Matches the compaction headroom of the smallest shipped window, so the
# default fails closed rather than letting an enormous body through.
FALLBACK_SKILL_BUDGET_TOKENS = 20_000

# Same stem rule as command names: what the composer and the load_skill
# argument can spell without quoting tricks.
_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*\n?", re.DOTALL)

_SKILL_FILE = "SKILL.md"


@dataclass(frozen=True)
class SkillFile:
    """One discovered skill."""

    name: str
    path: Path
    source: Literal["workspace", "user"]
    # True when this skill serves only because the tree had none of ours.
    fallback: bool = False
    # From frontmatter. ``whenToUse`` steers the brain's choice to load;
    # both may be empty — the skill still works, the catalog row is just
    # less helpful.
    description: str = ""
    when_to_use: str = ""

    @property
    def display_path(self) -> str:
        """POSIX-separated path for listings, ~-shortened for user files."""
        text = self.path.as_posix()
        if self.source == "user":
            home_text = Path.home().as_posix()
            if text.startswith(home_text):
                text = "~" + text[len(home_text) :]
        return text


@dataclass(frozen=True)
class LoadedSkill:
    """A skill whose body was loaded into this session (TD-4502).

    Recorded by both load paths — the ``load_skill`` tool and an invoked
    ``/name`` — so the inspector can list them apart from steering.
    Re-loading a name overwrites the entry: the last body is the one the
    conversation actually carries.
    """

    name: str
    source: Literal["workspace", "user"]
    path: str
    tokens: int


def global_skills_dir(home: str | Path) -> Path:
    """``~/.tstdesk/skills`` — the user-global tree."""
    return Path(home) / ".tstdesk" / "skills"


def _parse_frontmatter(text: str) -> dict[str, str]:
    """The two metadata keys we read, from a leading YAML document.

    Unknown keys are ignored rather than refused: the frontmatter exists
    for the catalog, and a skill that carries an extra note about itself
    still loads fine. What is NOT forgiven is unparseable YAML — that
    turns into an empty mapping and the caller decides.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        log.warning(
            "skill frontmatter is not valid YAML",
            extra={"extra_fields": {"error": str(exc)}},
        )
        return {}
    if not isinstance(loaded, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("description", "whenToUse"):
        value = loaded.get(key)
        if isinstance(value, str):
            out[key] = value.strip()
    return out


def _discover_tree(root: Path) -> dict[str, Path]:
    """Skill name → SKILL.md path under one skills root.

    Only real directories carrying a SKILL.md count. Containment is
    resolved and checked like commands: a symlinked skill directory that
    escapes the tree is skipped rather than followed.
    """
    try:
        if not root.is_dir():
            return {}
        root = root.resolve()
    except OSError as exc:
        log.warning(
            "skills directory unreadable, skipped",
            extra={"extra_fields": {"path": str(root), "error": str(exc)}},
        )
        return {}
    found: dict[str, Path] = {}
    for entry in sorted(root.iterdir()):
        name = entry.name
        if _NAME_RE.fullmatch(name) is None:
            continue
        try:
            skill_md = entry / _SKILL_FILE
            if not skill_md.is_file():
                continue
            resolved = skill_md.resolve()
        except OSError as exc:
            log.warning(
                "skill unreadable, skipped",
                extra={"extra_fields": {"path": str(entry), "error": str(exc)}},
            )
            continue
        try:
            resolved.relative_to(root)
        except ValueError:
            log.warning(
                "skill path escaped its skills directory, skipped",
                extra={"extra_fields": {"path": str(entry), "resolved": str(resolved)}},
            )
            continue
        found[name] = resolved
    return found


def discover_skills(workspace: str | Path, home_dir: str | Path | None = None) -> list[SkillFile]:
    """List the skills available for *workspace*, best-name-first.

    User-global wins on a shared name (as with commands). Each tree falls
    back to its ``.claude/skills`` counterpart only when it holds no
    skills of ours.
    """
    home = Path(home_dir).expanduser() if home_dir is not None else Path.home()
    ours_ws = _discover_tree(Path(workspace) / ".tst" / "skills")
    fallback_ws = {} if ours_ws else _discover_tree(Path(workspace) / ".claude" / "skills")
    ours_user = _discover_tree(global_skills_dir(home))
    fallback_user = {} if ours_user else _discover_tree(home / ".claude" / "skills")

    merged: dict[str, SkillFile] = {}
    # Lower precedence first so a later insert replaces it on collision.
    layers: tuple[tuple[dict[str, Path], Literal["workspace", "user"], bool], ...] = (
        (fallback_ws, "workspace", True),
        (ours_ws, "workspace", False),
        (fallback_user, "user", True),
        (ours_user, "user", False),
    )
    for names, source, fallback in layers:
        for name in sorted(names):
            try:
                meta = _parse_frontmatter(names[name].read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                # Unreadable now means an error at load time; discovery
                # still lists it so the refusal can name the file.
                meta = {}
            merged[name] = SkillFile(
                name=name,
                path=names[name],
                source=source,
                fallback=fallback,
                description=meta.get("description", ""),
                when_to_use=meta.get("whenToUse", ""),
            )
    return [merged[name] for name in sorted(merged)]


def read_skill_body(path: Path) -> str:
    """Read one SKILL.md, stripping the frontmatter.

    The frontmatter is catalog metadata; the brain reads prose, not
    YAML (same reasoning as command bodies).
    """
    text = path.read_text(encoding="utf-8")
    return _FRONTMATTER_RE.sub("", text, count=1)


def build_skills_catalog(skills: list[SkillFile]) -> str | None:
    """Render the post-prefix catalog block, or ``None`` when empty.

    Names and one-line metadata only — the whole point of progressive
    disclosure is that bodies stay out until something loads one.
    """
    if not skills:
        return None
    rows: list[str] = []
    for skill in skills:
        row = f"- {skill.name}"
        if skill.description:
            row += f": {skill.description}"
        if skill.when_to_use:
            row += f" (use when: {skill.when_to_use})"
        rows.append(row)
    header = (
        "<!-- skills -->\n"
        "Skills for this workspace. Call load_skill with a skill's name to\n"
        "read its full body before following it; /name in a user message\n"
        "expands it too."
    )
    return header + "\n" + "\n".join(rows)


def render_loaded_skill(skill: SkillFile, body: str) -> str:
    """Render a loaded body for a ``load_skill`` tool result.

    Plain delimiter lines, matching how command bodies splice: a fence
    would need escaping the moment the body held one. The provenance
    path stays at the top so the transcript shows where prose came from.
    """
    return (
        f"--- skill: {skill.name} ({skill.display_path}) ---\n{body.strip()}\n--- end of skill ---"
    )
