"""Skill discovery (TD-4502, TD-5011).

Finds and parses ``SKILL.md`` files for :mod:`tstd.context.skills`, which
renders the catalog and loads bodies.  Human-authored ``SKILL.md`` files
live under ``.tst/skills/<name>/`` and ``~/.tstdesk/skills/<name>/``.
User-global wins on the same ``<name>``.  When both of *our* trees have
no ``SKILL.md``, fall back to ``.claude/skills/``, and to
``~/.claude/skills/`` only when ``steering.claude_global_fallback`` is true
(TD-4845's key: Claude Code's global files are opt-in).

Frontmatter (TD-5011): ``description`` is required; ``whenToUse`` falls
back to it.  ``name`` is accepted, and the directory name wins when they
differ.  Claude Code keys that only describe a skill or grant it
something are ignored; a key that would restrict or reroute it, and any
unknown key, still skips the skill (TD-4502: a partial skill cannot load).
"""

from __future__ import annotations

import asyncio
import re
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

#: Keys a skill reads.  Only ``description`` is required (TD-5011).
_HONORED_FRONTMATTER = frozenset({"name", "description", "whenToUse"})
#: Claude Code keys dropped without effect (TD-5011).  They label the
#: skill (``license``, ``version``, ``metadata``, ``compatibility``,
#: ``argument-hint``) or pre-approve tools (``allowed-tools``); without the
#: grant, the skill's tool calls go through this workspace's own policy.
_IGNORED_FRONTMATTER = frozenset(
    {"license", "version", "metadata", "compatibility", "argument-hint", "allowed-tools"}
)
#: Claude Code switches ignored at their default value only.  Any other
#: value limits who may invoke the skill, which the catalog cannot honor.
_DEFAULT_ONLY_FRONTMATTER: dict[str, bool] = {
    "disable-model-invocation": False,
    "user-invocable": True,
}
_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: (message, path, details) already logged: discovery runs on every brain
#: prompt, and one skipped skill must not log once per turn.
_WARNED: set[tuple[str, str, str]] = set()


@dataclass(frozen=True)
class Skill:
    """One discovered skill.

    ``body`` is the markdown after frontmatter.  ``tokens`` is the
    heuristic count of that body (the over-budget check uses the same
    figure).  ``directory`` is where the skill lives as the model should
    read it: workspace-relative, or ``~/...`` for a user-global skill.
    """

    name: str
    description: str
    when_to_use: str
    source: SkillSource
    body: str
    path: Path
    tokens: int
    directory: str


class SkillDiscoverer:
    """Finds ``SKILL.md`` files for a workspace.

    Args:
        home_dir: Override the user home directory (test seam).
            Defaults to the real user home.
        claude_global_fallback: ``steering.claude_global_fallback``.  When
            false, ``~/.claude/skills/`` is not read (TD-5011, as TD-4845).
    """

    def __init__(
        self, home_dir: str | Path | None = None, *, claude_global_fallback: bool = False
    ) -> None:
        self._home = Path(home_dir).expanduser() if home_dir is not None else Path.home()
        self._claude_global_fallback = claude_global_fallback

    @property
    def home_dir(self) -> Path:
        """The home directory used for user-global skill trees."""
        return self._home

    def discover(self, workspace_path: str | Path) -> list[Skill]:
        """Discover skills for *workspace_path*, user-global winning on name.

        Missing trees are not errors.  Invalid directory names are
        skipped.  A missing ``description``, or a frontmatter key that
        cannot be honored or ignored, skips that skill.
        """
        workspace = Path(workspace_path).expanduser().resolve()
        ours_ws = self._skill_files(workspace / ".tst" / "skills")
        ours_user = self._skill_files(self._home / ".tstdesk" / "skills")
        if ours_ws or ours_user:
            return self._merge(
                self._parse_tree(ours_ws, "workspace", workspace),
                self._parse_tree(ours_user, "user", self._home),
            )
        # On the home folder, `.claude/skills` is the workspace's own tree.
        claude_ws = self._skill_files(workspace / ".claude" / "skills")
        claude_user = (
            self._skill_files(self._home / ".claude" / "skills")
            if self._claude_global_fallback
            else []
        )
        return self._merge(
            self._parse_tree(claude_ws, "claude_workspace", workspace),
            self._parse_tree(claude_user, "claude_user", self._home),
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

    def _parse_tree(self, files: list[Path], source: SkillSource, root: Path) -> dict[str, Skill]:
        found: dict[str, Skill] = {}
        for path in files:
            parsed = self._parse_file(path, source, _display_dir(path.parent, root, self._home))
            if parsed is not None:
                found[parsed.name] = parsed
        return found

    def _parse_file(self, path: Path, source: SkillSource, directory: str) -> Skill | None:
        name = path.parent.name
        if not _SKILL_NAME.match(name):
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        metadata, body = parse_frontmatter(raw)
        refused = sorted(str(key) for key in metadata if not _key_is_accepted(key, metadata[key]))
        if refused:
            warn_once("skill skipped: frontmatter keys it cannot honor", path, keys=refused)
            return None
        raw_desc = metadata.get("description")
        raw_when = metadata.get("whenToUse")
        raw_name = metadata.get("name")
        if not isinstance(raw_desc, str) or not raw_desc.strip():
            return None
        if raw_when is not None and not isinstance(raw_when, str):
            return None
        if raw_name is not None and not isinstance(raw_name, str):
            return None
        if raw_name is not None and raw_name.strip() != name:
            # The directory is what `/name`, `load_skill` and the tree merge
            # key on, so it wins; the mismatch is logged, not fatal (TD-5011).
            warn_once(
                "skill name differs from its directory; using the directory",
                path,
                name=raw_name.strip(),
            )
        description = raw_desc.strip()
        when_to_use = raw_when.strip() if raw_when is not None else ""
        return Skill(
            name=name,
            description=description,
            when_to_use=when_to_use or description,
            source=source,
            body=body,
            path=path,
            tokens=heuristic_count(body).count,
            directory=directory,
        )

    @staticmethod
    def _merge(lower: dict[str, Skill], higher: dict[str, Skill]) -> list[Skill]:
        """*higher* wins on the same name. Result is sorted by name."""
        merged = {**lower, **higher}
        return [merged[name] for name in sorted(merged)]


def _display_dir(directory: Path, root: Path, home: Path) -> str:
    """Workspace-relative, or ``~/...`` under home (TD-1104: no absolute home)."""
    if root != home:
        return directory.relative_to(root).as_posix()
    return f"~/{directory.relative_to(home).as_posix()}"


def warn_once(message: str, path: Path, **fields: object) -> None:
    """Log a skipped or odd skill once per process, not on every prompt."""
    key = (message, str(path), repr(sorted(fields.items())))
    if key in _WARNED:
        return
    _WARNED.add(key)
    log.warning(message, extra={"extra_fields": {"path": str(path), **fields}})


def _key_is_accepted(key: object, value: object) -> bool:
    """Whether a frontmatter key lets the skill load (TD-4502, TD-5011)."""
    if key in _HONORED_FRONTMATTER or key in _IGNORED_FRONTMATTER:
        return True
    if isinstance(key, str) and key in _DEFAULT_ONLY_FRONTMATTER:
        return value is _DEFAULT_ONLY_FRONTMATTER[key]
    return False


def list_workspace_skills(
    workspace: str | Path,
    home_dir: str | Path | None = None,
    *,
    claude_global_fallback: bool = False,
) -> list[Skill]:
    """Synchronous discovery — wrap in ``asyncio.to_thread`` on the I/O path."""
    discoverer = SkillDiscoverer(home_dir=home_dir, claude_global_fallback=claude_global_fallback)
    return discoverer.discover(workspace)


async def list_workspace_skills_async(
    workspace: str | Path,
    home_dir: str | Path | None = None,
    *,
    claude_global_fallback: bool = False,
) -> list[Skill]:
    """Discover skills off the event loop."""
    return await asyncio.to_thread(
        list_workspace_skills,
        workspace,
        home_dir,
        claude_global_fallback=claude_global_fallback,
    )


def session_claude_global_fallback(session: object) -> bool:
    """``steering.claude_global_fallback`` from the session's config (default false)."""
    steering = getattr(getattr(session, "config", None), "steering", None)
    return getattr(steering, "claude_global_fallback", False) is True
