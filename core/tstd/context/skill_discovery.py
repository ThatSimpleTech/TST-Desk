"""Skill discovery (TD-4502, TD-5011, TD-5107).

Finds and parses ``SKILL.md`` files for :mod:`tstd.context.skills`, which
renders the catalog and loads bodies.  Human-authored ``SKILL.md`` files
live under ``.tst/skills/<name>/`` and ``~/.tstdesk/skills/<name>/``.
User-global wins on the same ``<name>``.  When both of *our* trees have
no ``SKILL.md``, fall back to ``.claude/skills/``, and to
``~/.claude/skills/`` only when ``steering.claude_global_fallback`` is true
(TD-4845's key: Claude Code's global files are opt-in).

Frontmatter (TD-5011, TD-5107): ``description`` is required;
``whenToUse`` (or Claude Code's ``when_to_use``) falls back to it.
``name`` is accepted, and the directory name wins when they differ.
``compatibility`` is shown to the model beside the description, and
``disable-model-invocation`` keeps a skill out of the model's reach
(``/name`` still loads it).  Pure labels pass silently.  Every other key,
known to another tool or unknown, no longer skips the skill: it loads, and
the keys it ignored are logged and listed for the inspector and Doctor
(the Agent Skills spec: load and warn, do not reject).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..logging import get_logger
from .frontmatter import parse_frontmatter
from .tokens import heuristic_count

log = get_logger("tstd.skills")

SkillSource = Literal["workspace", "user", "claude_workspace", "claude_user"]

# Fixed body cap. Recorded in DECISIONS.md (TD-4502).
SKILL_BODY_TOKEN_CAP = 4000

#: Keys a skill reads (TD-4502, TD-5011, TD-5107).  ``description`` is the
#: one required key.
_READ_FRONTMATTER = frozenset(
    {
        "name",
        "description",
        "whenToUse",
        "when_to_use",
        "compatibility",
        "disable-model-invocation",
    }
)
#: Labels: they describe the skill and ask nothing of the client, so
#: passing them over drops nothing and they are not listed as ignored.
_LABEL_FRONTMATTER = frozenset({"license", "version", "metadata", "argument-hint"})
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
    #: Agent Skills ``compatibility`` (environment needs), or "" (TD-5107).
    compatibility: str = ""
    #: ``disable-model-invocation`` set: only the user's ``/name`` loads it.
    user_only: bool = False
    #: Frontmatter keys present but not acted on, sorted (TD-5107).
    ignored_keys: tuple[str, ...] = ()


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
        skipped.  A missing ``description``, or a ``name`` or when-to-use
        that is not a string, skips that skill.  Any other key loads with
        the skill and is listed in :attr:`Skill.ignored_keys` (TD-5107).
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
        except UnicodeDecodeError:
            # Not UTF-8 text (a Windows-1252 file, or UTF-16 from PowerShell
            # 5.1 `Out-File`). Its frontmatter cannot be read, so it is
            # skipped like an unreadable file. Raising here would take down
            # every caller: prompt assembly, the inspector, Doctor.
            warn_once("skill skipped; SKILL.md is not UTF-8 text", path)
            return None
        metadata, body = parse_frontmatter(raw)
        fields = _read_frontmatter(metadata)
        if isinstance(fields, str):
            # Named, so a skill that vanishes from the inspector and Doctor
            # leaves a trace (TD-5160).
            warn_once("skill skipped; frontmatter key missing or not text", path, key=fields)
            return None
        raw_name = metadata.get("name")
        if isinstance(raw_name, str) and raw_name.strip() != name:
            # The directory is what `/name`, `load_skill` and the tree merge
            # key on, so it wins; the mismatch is logged, not fatal (TD-5011).
            warn_once(
                "skill name differs from its directory; using the directory",
                path,
                name=raw_name.strip(),
            )
        if fields.ignored_keys:
            warn_once(
                "skill loaded; frontmatter keys ignored",
                path,
                keys=list(fields.ignored_keys),
            )
        return Skill(
            name=name,
            description=fields.description,
            when_to_use=fields.when_to_use or fields.description,
            source=source,
            body=body,
            path=path,
            tokens=heuristic_count(body).count,
            directory=directory,
            compatibility=fields.compatibility,
            user_only=fields.user_only,
            ignored_keys=fields.ignored_keys,
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


@dataclass(frozen=True)
class _Frontmatter:
    description: str
    when_to_use: str
    compatibility: str
    user_only: bool
    ignored_keys: tuple[str, ...]


def _read_frontmatter(metadata: dict[Any, Any]) -> _Frontmatter | str:
    """The fields a skill uses, or the key that stops it loading.

    Only a missing description, or a ``name`` or when-to-use that is not a
    string, refuses the skill.  Anything else present but not acted on is
    returned in ``ignored_keys`` (TD-5107).
    """
    raw_desc = metadata.get("description")
    if not isinstance(raw_desc, str) or not raw_desc.strip():
        return "description"
    raw_name = metadata.get("name")
    if raw_name is not None and not isinstance(raw_name, str):
        return "name"
    ours = metadata.get("whenToUse")
    claude = metadata.get("when_to_use")
    for key, value in (("whenToUse", ours), ("when_to_use", claude)):
        if value is not None and not isinstance(value, str):
            return key
    ours_text = ours.strip() if isinstance(ours, str) else ""
    claude_text = claude.strip() if isinstance(claude, str) else ""
    ignored: list[str] = []
    if ours_text and claude_text and claude_text != ours_text:
        # Our own key wins; the other is named so the loss is visible.
        ignored.append("when_to_use")
    raw_compat = metadata.get("compatibility")
    if raw_compat is not None and not isinstance(raw_compat, str):
        ignored.append("compatibility")
    compatibility = raw_compat.strip() if isinstance(raw_compat, str) else ""
    dmi = metadata.get("disable-model-invocation")
    for key, value in metadata.items():
        if key in _READ_FRONTMATTER or key in _LABEL_FRONTMATTER:
            continue
        if key == "user-invocable" and (value is True or value is None):
            continue  # the default: every skill is the user's to invoke
        ignored.append(str(key))
    return _Frontmatter(
        description=raw_desc.strip(),
        when_to_use=ours_text or claude_text,
        compatibility=compatibility,
        # The strict reading: any value but YAML false or no value at all
        # keeps the skill out of the model's reach. A quoted '' or "false"
        # is a value, so it counts (TD-5160 aligned the docs to this).
        user_only=not (dmi is False or dmi is None),
        ignored_keys=tuple(sorted(ignored)),
    )


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
