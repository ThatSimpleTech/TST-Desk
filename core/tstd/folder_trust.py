"""A project's own instructions load only once its folder is trusted (SECUX2-01).

Opening a folder used to load its AGENTS.md or CLAUDE.md, ``.tst/rules``,
every SKILL.md and the workers under ``.tst/agents`` straight into the
model's prompt, with no gate and no word: a cloned repository could steer
the agent before anyone read what it said. The ``.tst/config.yaml`` trust
card (TD-5113) never covered them.

The manager's rules:

- Trust is per folder, keyed by its real path (a symlink or a renamed
  folder inherits nothing), and covers later edits to the kinds of file that
  were there when it was given.
- A kind that appears later (a first SKILL.md, a first worker) asks again,
  listing only the new files; the kinds already trusted keep loading.
- Nothing is grandfathered: a folder with such files and no decision loads
  none of them until the person decides.
- A run nobody is watching (a scheduled job, ``tst run``) still runs, without
  the files, and says so; it never waits on the card.

The user's own ``~/.tstdesk`` files are not a project's and always load.
Decisions live in the data folder, never in the project, so a repository
cannot ship its own trust.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from .data_version import read_only
from .logging import get_logger

log = get_logger("tstd.folder_trust")

TRUST_FILE = "folder_trust.yaml"

Kind = Literal["instructions", "rules", "skills", "workers"]
KINDS: tuple[Kind, ...] = ("instructions", "rules", "skills", "workers")
State = Literal["none", "pending", "trusted", "declined"]

#: How deep a nested AGENTS.md or CLAUDE.md is looked for, and folders skipped.
_NESTED_DEPTH = 6
#: At most this many folders are looked in for nested files, so a huge tree
#: costs a bounded walk (the 0.5.4 start-up fix).
_NESTED_MAX_DIRS = 2000
#: A folder's view is reused for this long; a decision clears it at once.
_VIEW_TTL_SECONDS = 15.0
_SKIP_DIRS = frozenset({".git", ".tst", "node_modules", ".venv", "venv", "__pycache__", "target"})


@dataclass(frozen=True)
class ProjectFile:
    kind: Kind
    path: str  # relative to the project folder, POSIX


@dataclass(frozen=True)
class FolderView:
    """What the card shows for one project folder."""

    folder: str
    state: State
    files: tuple[
        ProjectFile, ...
    ]  # the files the card asks about (only new ones when some kinds are trusted)
    trusted_kinds: frozenset[Kind]


def real(folder: str | Path) -> str:
    return os.path.realpath(os.path.expanduser(str(folder)))


def project_files(folder: str | Path) -> list[ProjectFile]:
    """Every file of the four kinds in *folder*, as the loaders would find them."""
    root = Path(real(folder))
    found: list[ProjectFile] = []

    def add(kind: Kind, path: Path) -> None:
        found.append(ProjectFile(kind, path.relative_to(root).as_posix()))

    for name in ("AGENTS.md", "CLAUDE.md"):
        if (root / name).is_file():
            add("instructions", root / name)
            break
    for path in _nested_instructions(root):
        add("instructions", path)
    rules = root / ".tst" / "rules"
    if rules.is_dir():
        for path in sorted(rules.glob("*.md")):
            if path.is_file():
                add("rules", path)
    for tree in (root / ".tst" / "skills", root / ".claude" / "skills"):
        if tree.is_dir():
            for child in sorted(tree.iterdir()):
                if (child / "SKILL.md").is_file():
                    add("skills", child / "SKILL.md")
    agents = root / ".tst" / "agents"
    if agents.is_dir():
        for path in sorted(agents.glob("*.md")):
            if path.is_file():
                add("workers", path)
    return found


def _nested_instructions(root: Path) -> list[Path]:
    out: list[Path] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        seen += 1
        if seen > _NESTED_MAX_DIRS:
            break
        here = Path(dirpath)
        depth = len(here.relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        if depth >= _NESTED_DEPTH:
            dirnames[:] = []
        if here == root:
            continue
        for name in ("AGENTS.md", "CLAUDE.md"):
            if name in filenames:
                out.append(here / name)
                break
    return out


class FolderTrustStore:
    """Decisions per project folder, in the data folder's ``folder_trust.yaml``."""

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir) / TRUST_FILE
        self._lock = threading.Lock()
        self._aliases: dict[str, str] = {}
        self._lookup: Callable[[str], str | None] | None = None
        self._views: dict[str, tuple[float, FolderView]] = {}

    # ── records ───────────────────────────────────────────────────────

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, yaml.YAMLError) as exc:
            log.warning("folder_trust.yaml unreadable", extra={"extra_fields": {"error": str(exc)}})
            return {}
        folders = raw.get("folders") if isinstance(raw, dict) else None
        return (
            {str(k): v for k, v in folders.items() if isinstance(v, dict)}
            if isinstance(folders, dict)
            else {}
        )

    def _save(self, records: dict[str, dict[str, object]]) -> None:
        if read_only() is not None:  # UPGRADE-02: a newer version's folder
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".folder_trust.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"folders": records}, handle, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _record(self, folder: str) -> tuple[str | None, frozenset[Kind]]:
        rec = self._load().get(folder)
        if rec is None:
            return None, frozenset()
        decision = rec.get("decision")
        kinds = rec.get("kinds")
        trusted = frozenset(k for k in (kinds if isinstance(kinds, list) else ()) if k in KINDS)
        return (decision if decision in ("trusted", "declined") else None), trusted

    # ── worktrees ─────────────────────────────────────────────────────

    def alias(self, workspace: str | Path, project: str | Path) -> None:
        """A chat's worktree reads its project's trust."""
        if real(workspace) != real(project):
            self._aliases[real(workspace)] = real(project)

    def set_project_lookup(self, lookup: Callable[[str], str | None]) -> None:
        """The daemon's answer to "which project is this chat's worktree of"."""
        self._lookup = lookup

    def project_of(self, workspace: str | Path) -> str:
        key = real(workspace)
        if key in self._aliases:
            return self._aliases[key]
        found = self._lookup(key) if self._lookup is not None else None
        return real(found) if found else key

    # ── questions ─────────────────────────────────────────────────────

    def allows(self, workspace: str | Path, kind: Kind) -> bool:
        decision, kinds = self._record(self.project_of(workspace))
        return decision == "trusted" and kind in kinds

    def view(self, workspace: str | Path) -> FolderView:
        folder = self.project_of(workspace)
        view = self._compute_view(folder)
        self._views[folder] = (time.monotonic(), view)
        return view

    def recent_view(self, workspace: str | Path) -> FolderView:
        """The view, reusing one computed in the last few seconds.

        For the chat snapshot only, where reviving many chats in one folder
        tree would walk it once per chat. Loading never reads this: it asks
        `allows`, so a new kind of file still waits for the card.
        """
        folder = self.project_of(workspace)
        cached = self._views.get(folder)
        if cached is not None and time.monotonic() - cached[0] < _VIEW_TTL_SECONDS:
            return cached[1]
        return self.view(folder)

    def _compute_view(self, folder: str) -> FolderView:
        files = project_files(folder)
        decision, kinds = self._record(folder)
        if not files:
            return FolderView(folder, "none", (), kinds)
        if decision == "declined":
            return FolderView(folder, "declined", tuple(files), kinds)
        new = [f for f in files if decision != "trusted" or f.kind not in kinds]
        if decision == "trusted" and not new:
            return FolderView(folder, "trusted", (), kinds)
        return FolderView(
            folder, "pending", tuple(new), kinds if decision == "trusted" else frozenset()
        )

    def decide(self, workspace: str | Path, trusted: bool) -> FolderView:
        folder = self.project_of(workspace)
        with self._lock:
            records = self._load()
            if trusted:
                kinds = sorted({f.kind for f in project_files(folder)})
                records[folder] = {"decision": "trusted", "kinds": kinds}
            else:
                records[folder] = {"decision": "declined", "kinds": []}
            self._save(records)
        self._views.pop(folder, None)
        return self.view(folder)

    def revoke(self, folder: str | Path) -> None:
        key = real(folder)
        self._views.pop(key, None)
        with self._lock:
            records = self._load()
            records.pop(key, None)
            records.pop(str(folder), None)
            self._save(records)

    def trusted_folders(self) -> list[tuple[str, list[str]]]:
        out: list[tuple[str, list[str]]] = []
        for folder, rec in sorted(self._load().items()):
            if rec.get("decision") == "trusted":
                kinds = rec.get("kinds")
                out.append((folder, [str(k) for k in kinds] if isinstance(kinds, list) else []))
        return out


# ── the loaders' gate ────────────────────────────────────────────────

_store: FolderTrustStore | None = None


def bind(store: FolderTrustStore | None) -> None:
    """The daemon's store. Unbound (a library caller, a test) loads everything as before."""
    global _store
    _store = store


def bound() -> FolderTrustStore | None:
    return _store


def allows(workspace: str | Path, kind: Kind) -> bool:
    """Whether the project's own files of *kind* may load for *workspace*."""
    return True if _store is None else _store.allows(workspace, kind)


def withheld(workspace: str | Path) -> list[ProjectFile]:
    """The project files a run in *workspace* goes without, for a one-line note."""
    if _store is None:
        return []
    view = _store.view(workspace)
    return list(view.files) if view.state in ("pending", "declined") else []


def kinds_label(files: Iterable[ProjectFile]) -> str:
    names = {
        "instructions": "AGENTS.md",
        "rules": "rules",
        "skills": "skills",
        "workers": "workers",
    }
    return ", ".join(dict.fromkeys(names[f.kind] for f in files))
