"""A git worktree per session (TD-5126): the git side.

Two sessions in one repository used to share one working tree, so each
saw the other's half-made edits and a test run in one broke the other.
Now a session in a git repository gets its own worktree, on a new branch
from the main checkout's ``HEAD``, in a folder outside the repository.

Every git call goes through :func:`tstd.git_raw.run_git`, so hooks,
fsmonitor and implicit bare repos stay off (TD-5108), and every call
that writes or reads the working tree pins the configured filter drivers
off (TD-5157). The checkout of a new worktree is one of those calls: a
file a filter would rewrite (git-lfs, for one) is checked out as git
stores it. ``worktree.setup`` (``git lfs pull``, say) is the user's way
to run their own filters there.

This module knows git and the file system only. Which sessions get a
worktree, and what happens to the session around these calls, is
:mod:`tstd.worktree_session`.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from .git_raw import GitRawError, checked_git, filter_pins, run_git
from .git_stage import commit_on_head
from .proc_lifecycle import finish_subprocess

#: Branches the daemon creates for worktrees: ``tst/wt/<name>``.
BRANCH_PREFIX = "tst/wt/"
#: Gitignore-syntax list of ignored files to copy into each new worktree.
INCLUDE_FILE = Path(".tst") / "worktree-include"
#: The workspace config, copied over when the repository does not track it.
WORKSPACE_CONFIG = Path(".tst") / "config.yaml"

_ADD_TIMEOUT = 300.0
_SETUP_NOTE_CHARS = 2000

SetupState = Literal["none", "running", "ok", "failed", "skipped"]
CloseAction = Literal["keep", "merge", "remove"]


class WorktreeError(RuntimeError):
    """A worktree step failed; the message is fit to show the user."""


@dataclass(frozen=True)
class RepoSpot:
    """Where a workspace sits in its repository."""

    top: Path  # the main checkout's top folder
    prefix: str  # the workspace below ``top``, posix, "" at the top
    head: str  # the commit ``HEAD`` names
    in_head: bool = True  # ``HEAD`` has ``prefix``; a worktree would have its files


@dataclass
class SessionWorktree:
    """One session's worktree, as the daemon keeps and persists it."""

    path: str  # the worktree's top folder
    branch: str
    project: str  # the workspace the user opened; the rail groups by it
    repo_top: str  # the main checkout's top folder
    prefix: str  # the project below ``repo_top``; the session works there
    base: str  # the commit the branch started from
    main_dirty: int = 0  # changed paths in the main checkout at creation
    setup: SetupState = "none"
    setup_note: str = ""
    # git tracks the workspace config here. When it does not, the worktree
    # holds a copy of the project's file and TST Desk saves to the project's.
    config_tracked: bool = False

    @property
    def root(self) -> Path:
        """The session's working folder: the project's spot in the worktree."""
        return Path(self.path) / self.prefix if self.prefix else Path(self.path)

    def to_record(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_record(cls, data: Mapping[str, Any] | None) -> SessionWorktree | None:
        """Rebuild from a store row; ``None`` for no row or a garbled one."""
        if not isinstance(data, Mapping):
            return None
        try:
            found = cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})
        except TypeError:
            return None
        if found.setup == "running":
            # A restart cut the setup command off: say so rather than spin.
            found = replace(found, setup="failed", setup_note="Setup was cut off by a restart.")
        return found


@dataclass(frozen=True)
class WorktreeChanges:
    """What a worktree holds that its base did not."""

    files: int  # uncommitted paths, untracked included, ignored not
    commits: int  # commits on the branch since the base

    @property
    def changed(self) -> bool:
        return self.files > 0 or self.commits > 0


def branch_for(name: str) -> str:
    return f"{BRANCH_PREFIX}{name}"


def folder_for(folder: Path, top: Path, name: str) -> Path:
    """``<folder>/<repo folder name>-<name>``, the name made path-safe."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", top.name).strip("-.") or "repo"
    return folder / f"{stem}-{name}"


async def find_repo(workspace: Path, env: Mapping[str, str]) -> RepoSpot | None:
    """The workspace's repository, or ``None`` when there is no worktree to make.

    ``None`` for a folder outside git, a bare repository, and a repository
    with no commit yet (there is no ``HEAD`` to branch from). ``in_head``
    is False for a folder ``HEAD`` does not have (new, untracked or
    ignored): a worktree's copy of it would be empty.
    """
    rc, out, _err = await run_git(
        workspace, "rev-parse", "--is-inside-work-tree", "--show-toplevel", "--show-prefix", env=env
    )
    if rc != 0:
        return None
    lines = os.fsdecode(out).splitlines()
    if len(lines) < 2 or lines[0].strip() != "true":
        return None
    prefix = lines[2].strip().rstrip("/") if len(lines) > 2 else ""
    rc, out, _err = await run_git(
        workspace, "rev-parse", "--verify", "-q", "HEAD^{commit}", env=env
    )
    if rc != 0 or not out.strip():
        return None
    head = out.decode().strip()
    top = await asyncio.to_thread(Path(lines[1].strip()).resolve)
    in_head = True
    if prefix:
        # ``<rev>:<path>`` reads the path from the top, whatever the cwd.
        rc, out, _err = await run_git(top, "cat-file", "-t", f"{head}:{prefix}", env=env)
        in_head = rc == 0 and out.strip() == b"tree"
    return RepoSpot(top=top, prefix=prefix, head=head, in_head=in_head)


#: ``.tst`` entries that are the user's shared content, not TST Desk's own.
_SHARED_TST = frozenset(
    {"rules", "skills", "commands", "context", "agents", "worktree-include"}  # agents: TD-5161
)


def _own_untracked(xy: str, path: str) -> bool:
    """An untracked file TST Desk planted in a ``.tst`` folder, not the user's work.

    The ``.tst/memory`` templates, the copied ``.tst/config.yaml``, the
    decisions ledger and attachment copies appear in every worktree. They
    neither make one "changed" nor travel back in a merge, where an
    untracked copy in the main checkout would stop it. Memory the user
    accepts is committed, so it still counts, and so does anything new in
    the shared ``rules``, ``skills``, ``commands``, ``context`` and
    ``agents`` folders.
    """
    if xy != "??":
        return False
    parts = path.split("/")
    if ".tst" not in parts[:-1]:
        return False
    after = parts[parts.index(".tst") + 1]
    return after not in _SHARED_TST


async def _status(top: Path, env: Mapping[str, str]) -> list[tuple[str, str]]:
    """``(XY, path)`` per changed path: untracked listed file by file.

    Ignored files and TST Desk's own untracked ``.tst`` files are left out.
    """
    pins = await filter_pins(top, env)
    out = await checked_git(
        top,
        *pins,
        "status",
        "--porcelain=v1",
        "-z",
        "--no-renames",
        "--untracked-files=all",
        "--ignore-submodules=dirty",
        env=env,
    )
    found: list[tuple[str, str]] = []
    for entry in out.split(b"\0"):
        if len(entry) > 3:
            xy, path = entry[:2].decode("ascii", "replace"), os.fsdecode(entry[3:])
            if not _own_untracked(xy, path):
                found.append((xy, path))
    return found


async def main_dirty_count(top: Path, env: Mapping[str, str]) -> int:
    """Changed paths in the main checkout that a new worktree will not have."""
    return len(await _status(top, env))


async def add_worktree(spot: RepoSpot, path: Path, branch: str, env: Mapping[str, str]) -> None:
    """``git worktree add -b <branch> <path> <HEAD>`` with filter drivers pinned off."""
    if await asyncio.to_thread(path.exists):
        raise WorktreeError(f"{path} already exists")
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    pins = await filter_pins(spot.top, env)
    rc, _out, err = await run_git(
        spot.top,
        *pins,
        "worktree",
        "add",
        "--quiet",
        "-b",
        branch,
        str(path),
        spot.head,
        env=env,
        timeout=_ADD_TIMEOUT,
    )
    if rc != 0:
        detail = err.decode("utf-8", "replace").strip()
        raise WorktreeError(
            f"git worktree add failed: {detail}" if detail else "git worktree add failed"
        )


async def copy_included(main_top: Path, include: Path, wt_top: Path, env: Mapping[str, str]) -> int:
    """Copy ignored files that ``include`` names from the main checkout. Returns the count.

    ``include`` uses gitignore syntax, read from the repository's top like
    ``.git/info/exclude``. A file is copied only when git also ignores it,
    so a tracked or plain untracked file is never duplicated. A file the
    worktree already has is left alone; a symlink is copied as a link.
    """
    if not await asyncio.to_thread(include.is_file):
        return 0
    out = await checked_git(
        main_top,
        "ls-files",
        "-z",
        "--others",
        "--ignored",
        f"--exclude-from={include}",
        env=env,
    )
    candidates = [os.fsdecode(n) for n in out.split(b"\0") if n and not n.endswith(b"/")]
    if not candidates:
        return 0
    rc, out, err = await run_git(
        main_top,
        "check-ignore",
        "-z",
        "--stdin",
        env=env,
        stdin=b"".join(os.fsencode(c) + b"\0" for c in candidates),
    )
    if rc not in (0, 1):
        raise WorktreeError(f"git check-ignore failed: {err.decode('utf-8', 'replace').strip()}")
    ignored = [os.fsdecode(n) for n in out.split(b"\0") if n]
    return await asyncio.to_thread(_copy_files, main_top, wt_top, ignored)


def _copy_files(src_top: Path, dst_top: Path, rels: list[str]) -> int:
    copied = 0
    for rel in rels:
        parts = Path(rel).parts
        if not parts or ".." in parts or Path(rel).is_absolute():
            continue
        src, dst = src_top / rel, dst_top / rel
        if os.path.lexists(dst) or not os.path.lexists(src):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=False)
        copied += 1
    return copied


async def run_setup(
    command: str,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,  # noqa: ASYNC109 - passed to finish_subprocess
) -> tuple[bool, str]:
    """Run the setup command once in *cwd*. ``(ok, the output's tail)``."""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=dict(env),
    )
    try:
        out, _err = await finish_subprocess(proc, timeout=timeout)
    except TimeoutError:
        return False, f"Setup stopped after {int(timeout)} s: {command}"
    text = out.decode("utf-8", "replace").strip()[-_SETUP_NOTE_CHARS:]
    if proc.returncode == 0:
        return True, text
    return False, f"Setup exited with {proc.returncode}: {command}\n{text}".strip()


async def changes(wt: SessionWorktree, env: Mapping[str, str]) -> WorktreeChanges:
    """Uncommitted paths in the worktree and commits on its branch since the base.

    A worktree folder deleted by hand has no uncommitted paths left to count.
    """
    top = Path(wt.repo_top)
    files = 0
    if await asyncio.to_thread(Path(wt.path).is_dir):
        files = len(await _status(Path(wt.path), env))
    rc, out, _err = await run_git(
        top, "rev-list", "--count", f"{wt.base}..refs/heads/{wt.branch}", env=env
    )
    commits = int(out.strip() or b"0") if rc == 0 else 0
    return WorktreeChanges(files=files, commits=commits)


async def commit_all(wt_top: Path, message: str, env: Mapping[str, str]) -> str | None:
    """Commit every uncommitted path in a worktree on its branch; ``None`` when clean."""
    paths = [wt_top / path for _xy, path in await _status(wt_top, env)]
    if not paths:
        return None
    return await commit_on_head(wt_top, paths, message, env=env)


async def remove(wt: SessionWorktree, env: Mapping[str, str], *, delete_branch: bool) -> None:
    """Remove the worktree folder (and the branch when asked), forcing past changes."""
    top = Path(wt.repo_top)
    if await asyncio.to_thread(Path(wt.path).exists):
        rc, _out, err = await run_git(
            top, "worktree", "remove", "--force", "--force", wt.path, env=env
        )
        if rc != 0:
            detail = err.decode("utf-8", "replace").strip()
            raise WorktreeError(f"git worktree remove failed: {detail}")
    await run_git(top, "worktree", "prune", env=env)
    if delete_branch:
        rc, _out, err = await run_git(top, "branch", "-D", wt.branch, env=env)
        if rc != 0 and b"not found" not in err:
            raise WorktreeError(f"git branch -D failed: {err.decode('utf-8', 'replace').strip()}")


async def _unfinished(top: Path, env: Mapping[str, str]) -> str | None:
    """The operation the main checkout is in the middle of, if any."""
    out = await checked_git(top, "rev-parse", "--git-dir", env=env)
    git_dir = top / os.fsdecode(out.strip())
    for name, what in (
        ("MERGE_HEAD", "a merge"),
        ("CHERRY_PICK_HEAD", "a cherry-pick"),
        ("REVERT_HEAD", "a revert"),
        ("rebase-merge", "a rebase"),
        ("rebase-apply", "a rebase"),
    ):
        if await asyncio.to_thread(os.path.lexists, git_dir / name):
            return what
    return None


async def merge_back(wt: SessionWorktree, message: str, env: Mapping[str, str]) -> str:
    """Commit the worktree's changes on its branch and merge it into the main checkout.

    Refused, with the main checkout untouched, when it is mid-merge or
    mid-rebase, when the merge would conflict, or when it has local
    changes the merge would overwrite. Returns a line for the user.
    """
    top = Path(wt.repo_top)
    if await asyncio.to_thread(Path(wt.path).is_dir):
        await commit_all(Path(wt.path), message, env)
    busy = await _unfinished(top, env)
    if busy is not None:
        raise WorktreeError(f"The main checkout is in the middle of {busy}. Finish it first.")
    rc, out, err = await run_git(
        top, "merge-tree", "--write-tree", "--quiet", "HEAD", wt.branch, env=env
    )
    if rc == 1:
        raise WorktreeError(
            f"{wt.branch} would conflict with the main checkout. "
            "Keep the worktree and merge it by hand."
        )
    if rc != 0:
        raise WorktreeError(f"git merge-tree failed: {err.decode('utf-8', 'replace').strip()}")
    pins = await filter_pins(top, env)
    rc, _out, err = await run_git(
        top, *pins, "merge", "--no-edit", "-m", f"Merge {wt.branch} (TST Desk)", wt.branch, env=env
    )
    if rc != 0:
        await run_git(top, "merge", "--abort", env=env)
        detail = err.decode("utf-8", "replace").strip() or os.fsdecode(out).strip()
        raise WorktreeError(f"git merge failed, nothing was changed: {detail}")
    return f"Merged {wt.branch} into the main checkout."


__all__ = [
    "BRANCH_PREFIX",
    "INCLUDE_FILE",
    "WORKSPACE_CONFIG",
    "CloseAction",
    "GitRawError",
    "RepoSpot",
    "SessionWorktree",
    "SetupState",
    "WorktreeChanges",
    "WorktreeError",
    "add_worktree",
    "branch_for",
    "changes",
    "commit_all",
    "copy_included",
    "find_repo",
    "folder_for",
    "main_dirty_count",
    "merge_back",
    "remove",
    "run_setup",
]
