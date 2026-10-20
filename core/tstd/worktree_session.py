"""A git worktree per session (TD-5126): the session side.

Which sessions get a worktree, what is copied into it, its setup command,
and what closing one does to the session. The git calls are in
:mod:`tstd.worktree`.

- A session or scheduled job opened on a folder in a git repository gets
  a worktree on ``tst/wt/<id>`` from the main checkout's ``HEAD``, unless
  ``worktree.enabled`` is off in Settings or in the folder's
  ``.tst/config.yaml``. The session then works in the worktree's copy of
  the folder; ``project_path`` is still the folder, so the rail groups the
  session with its project and New chat opens the next one there.
- The workspace config comes along when git does not track it, and a trust
  decision made on those exact bytes in the main checkout carries over.
  Ignored files ``.tst/worktree-include`` names are copied, and the
  config's ``worktree.setup`` runs once, only from a trusted file.
- Closing keeps, merges back or removes the worktree; an unchanged one is
  always removed. The session then works in the main checkout.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .boundary_config import WorktreeSection
from .config import ConfigError, WorktreeConfig
from .git_raw import GitRawError
from .logging import get_logger
from .protocol import SessionWorktreeInfo, WorktreeState
from .session import Session
from .tools.shell import sanitized_env
from .workspace_trust import TrustStore, read_view
from .worktree import (
    INCLUDE_FILE,
    WORKSPACE_CONFIG,
    CloseAction,
    SessionWorktree,
    WorktreeChanges,
    WorktreeError,
    add_worktree,
    branch_for,
    changes,
    commit_all,
    copy_included,
    find_repo,
    folder_for,
    main_dirty_count,
    merge_back,
    remove,
    run_setup,
)

log = get_logger("tstd.worktree")

_FAILURES = (WorktreeError, GitRawError, OSError, TimeoutError)


class WorktreeHasChanges(Exception):
    """Closing needs the user's choice: the worktree has changes."""

    def __init__(self, wt: SessionWorktree, found: WorktreeChanges) -> None:
        self.found = found
        parts = []
        if found.files:
            parts.append(f"{found.files} changed file{'s' if found.files != 1 else ''}")
        if found.commits:
            parts.append(f"{found.commits} commit{'s' if found.commits != 1 else ''}")
        super().__init__(
            f"The worktree on {wt.branch} has {' and '.join(parts)}. "
            "Keep it, merge it back, or remove it."
        )


@dataclass(frozen=True)
class WorkspaceWorktree:
    """The ``worktree`` section of a workspace's ``.tst/config.yaml``."""

    enabled: bool = True
    setup: str = ""  # from a file in force only
    setup_held: bool = False  # the file names a setup that is not in force


def _section(data: dict[str, Any] | None) -> WorktreeSection:
    raw = data.get("worktree") if isinstance(data, dict) else None
    try:
        return WorktreeSection.model_validate(raw or {})
    except ValidationError:
        return WorktreeSection()  # an invalid section applies nothing


def workspace_settings(project: str, trust: TrustStore | None) -> WorkspaceWorktree:
    """``enabled`` from the file as written; ``setup`` only once it is trusted."""
    try:
        view = read_view(project, trust)
    except ConfigError:
        return WorkspaceWorktree()
    written = _section(view.data)
    applied = _section(view.effective)
    setup = applied.setup.strip()
    return WorkspaceWorktree(
        enabled=written.enabled,
        setup=setup,
        setup_held=bool(written.setup.strip()) and not setup,
    )


def worktree_folder(config: WorktreeConfig, data_dir: Path) -> Path:
    """``worktree.folder``, or ``worktrees/`` in the data dir when it is empty."""
    folder = config.folder.strip()
    return Path(folder).expanduser() if folder else data_dir / "worktrees"


def info(wt: SessionWorktree | None) -> SessionWorktreeInfo | None:
    """The wire shape of a worktree."""
    if wt is None:
        return None
    return SessionWorktreeInfo(
        branch=wt.branch,
        path=str(wt.root),
        main_dirty=wt.main_dirty,
        setup=wt.setup,
        setup_note=wt.setup_note,
    )


def state_event(sess: Session, note: str = "") -> WorktreeState:
    return WorktreeState(session_id=sess.id, worktree=info(sess.worktree), note=note, seq=1)


def record_root(
    workspace_path: str, record: dict[str, Any] | None
) -> tuple[str, SessionWorktree | None]:
    """Where a stored session works: its worktree's copy of the folder, or the folder."""
    wt = SessionWorktree.from_record(record)
    return (str(wt.root), wt) if wt is not None else (workspace_path, None)


def _carry_config(project: str, root: Path, trust: TrustStore | None) -> None:
    """Copy an untracked workspace config over, and the trust decided on its bytes."""
    src, dst = Path(project) / WORKSPACE_CONFIG, root / WORKSPACE_CONFIG
    if src.is_file() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    if trust is None or not dst.is_file():
        return
    digest = hashlib.sha256(dst.read_bytes()).hexdigest()
    state = trust.state_for(project, digest)
    if state in ("trusted", "declined"):
        trust.record(root, digest, "trusted" if state == "trusted" else "declined")


@dataclass
class Opened:
    """What :func:`open_worktree` made: the worktree, the setup to run, a note."""

    worktree: SessionWorktree | None
    setup: str = ""
    note: str = ""


async def open_worktree(
    project: str,
    name: str,
    *,
    folder: Path,
    trust: TrustStore | None,
) -> Opened:
    """Make a worktree for a session opened on *project*, when one is wanted.

    No worktree, and no note, outside git or when the workspace turns it
    off. A git failure leaves the session in *project* with a note saying so.
    """
    settings = await asyncio.to_thread(workspace_settings, project, trust)
    if not settings.enabled:
        return Opened(None)
    env = sanitized_env()
    try:
        spot = await find_repo(Path(project), env)
    except _FAILURES:
        spot = None
    if spot is None:
        return Opened(None)
    path = folder_for(folder, spot.top, name)
    try:
        dirty = await main_dirty_count(spot.top, env)
        await add_worktree(spot, path, branch_for(name), env)
    except _FAILURES as e:
        log.warning(
            "worktree not made",
            extra={"extra_fields": {"workspace_path": project, "error": str(e)}},
        )
        return Opened(None, note=f"No worktree for this session ({e}). It works in {project}.")
    wt = SessionWorktree(
        path=str(path),
        branch=branch_for(name),
        project=project,
        repo_top=str(spot.top),
        prefix=spot.prefix,
        base=spot.head,
        main_dirty=dirty,
    )
    notes = [f"Working in a new worktree on {wt.branch}."]
    try:
        await asyncio.to_thread(_carry_config, project, wt.root, trust)
        copied = await copy_included(spot.top, Path(project) / INCLUDE_FILE, path, env)
        if copied:
            notes.append(
                f"Copied {copied} ignored file{'s' if copied != 1 else ''} "
                f"from {INCLUDE_FILE.as_posix()}."
            )
    except _FAILURES as e:
        notes.append(f"Copying ignored files failed: {e}")
    if settings.setup:
        wt.setup = "running"
    elif settings.setup_held:
        wt.setup = "skipped"
        wt.setup_note = (
            "worktree.setup is in a workspace config that is not trusted yet, so it did not run."
        )
    return Opened(wt, setup=settings.setup, note=" ".join(notes))


def start_setup(
    sess: Session,
    command: str,
    timeout: float,
    on_done: Callable[[Session], Awaitable[None]],
) -> asyncio.Task[None]:
    """Run the setup command in the background; the session's first turn waits for it."""
    ready = asyncio.Event()
    sess.worktree_ready = ready
    wt = sess.worktree
    assert wt is not None

    async def _run() -> None:
        ok, note = False, "Setup was stopped."
        try:
            ok, note = await run_setup(command, wt.root, sanitized_env(), timeout)
        except OSError as e:
            note = f"Setup did not run: {e}"
        finally:
            wt.setup, wt.setup_note = ("ok" if ok else "failed"), note
            ready.set()
        await on_done(sess)

    return asyncio.create_task(_run())


async def settle(wt: SessionWorktree) -> bool:
    """Remove *wt* when it has no changes. True when it was removed."""
    env = sanitized_env()
    try:
        if (await changes(wt, env)).changed:
            return False
        await remove(wt, env, delete_branch=True)
    except _FAILURES as e:
        log.warning(
            "worktree not settled", extra={"extra_fields": {"path": wt.path, "error": str(e)}}
        )
        return False
    return True


async def close(wt: SessionWorktree, action: CloseAction | None, *, title: str) -> str:
    """Keep, merge back or remove *wt*. Returns what happened, in a sentence.

    Raises:
        WorktreeHasChanges: *wt* has changes and *action* is None.
        WorktreeError: git refused; nothing that matters was changed.
    """
    env = sanitized_env()
    try:
        found = await changes(wt, env)
        if not found.changed:
            await remove(wt, env, delete_branch=True)
            return f"Removed the worktree on {wt.branch}: it had no changes."
        if action is None:
            raise WorktreeHasChanges(wt, found)
        if action == "keep":
            return f"Kept the worktree at {wt.path} on {wt.branch}."
        if action == "merge":
            line = await merge_back(wt, f"{title}\n\nTST Desk session on {wt.branch}.", env)
            await remove(wt, env, delete_branch=True)
            return line
        await remove(wt, env, delete_branch=True)
        return f"Removed the worktree on {wt.branch} and its changes."
    except (GitRawError, OSError, TimeoutError) as e:
        raise WorktreeError(str(e)) from e


async def open_child(parent: Session, child_id: str) -> SessionWorktree:
    """A delegate child's own worktree, from the parent's ``HEAD`` (TD-5126).

    Raises:
        WorktreeError: worktrees are off here, or this is not a git repository.
    """
    folder = parent.worktree_folder
    if folder is None:
        raise WorktreeError("worktrees are off on this computer")
    env = sanitized_env()
    try:
        spot = await find_repo(Path(parent.workspace_path), env)
        if spot is None:
            raise WorktreeError("this workspace is not a git repository with a commit")
        name = f"{parent.id[:8]}-{child_id[:8]}"
        path = folder_for(folder, spot.top, name)
        await add_worktree(spot, path, branch_for(name), env)
    except (GitRawError, OSError, TimeoutError) as e:
        raise WorktreeError(str(e)) from e
    wt = SessionWorktree(
        path=str(path),
        branch=branch_for(name),
        project=parent.workspace_path,
        repo_top=str(spot.top),
        prefix=spot.prefix,
        base=spot.head,
    )
    await asyncio.to_thread(_carry_config, parent.workspace_path, wt.root, parent.trust_store)
    return wt


async def settle_child(wt: SessionWorktree, task: str) -> str:
    """Put a finished child's work on its branch and remove the folder.

    Changes are committed on the child's branch, which stays for the
    parent to review and merge; an unchanged branch is deleted too.
    """
    env = sanitized_env()
    try:
        found = await changes(wt, env)
        if not found.changed:
            await remove(wt, env, delete_branch=True)
            return "The worker changed nothing in its worktree, so it was removed."
        line = task.strip().splitlines()[0][:72] if task.strip() else "delegated task"
        await commit_all(Path(wt.path), f"{line}\n\nTST Desk delegate on {wt.branch}.", env)
        await remove(wt, env, delete_branch=False)
    except (WorktreeError, GitRawError, OSError, TimeoutError) as e:
        return f"The worker's worktree at {wt.path} could not be tidied: {e}"
    return (
        f"The worker's changes are committed on branch {wt.branch}, not in your files. "
        f"Review with `git diff HEAD...{wt.branch}` and take them with `git merge {wt.branch}`."
    )


def rehome(sess: Session) -> SessionWorktree | None:
    """Forget the session's worktree; returns it. The caller moves the session."""
    wt = sess.worktree
    sess.worktree = None
    sess.worktree_ready = None
    return wt


__all__ = [
    "Opened",
    "WorkspaceWorktree",
    "WorktreeHasChanges",
    "close",
    "info",
    "open_child",
    "open_worktree",
    "record_root",
    "rehome",
    "settle",
    "settle_child",
    "start_setup",
    "state_event",
    "workspace_settings",
    "worktree_folder",
]
