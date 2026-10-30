"""A delegate child in its own git worktree (TD-5126).

``delegate`` with ``worktree: true`` gives the child a worktree on a new
branch from the parent's last commit, so its edits cannot collide with
the parent's. The child's tools are rebound to that folder: its paths are
guarded and judged against its own root, and memory commits land on its
branch. When it finishes, its changes are committed on the branch and the
folder is removed; the parent reviews and merges the branch through the
shell, under the classifier like any other command. A child that changed
nothing leaves no branch behind.

Workers side by side (TD-5195) open and settle their worktrees one at a
time per repository: ``git worktree add`` and the branch commits all
touch the main repository's own files.
"""

from __future__ import annotations

import asyncio
import os
import uuid
import weakref
from dataclasses import replace
from pathlib import Path

from ..git_raw import GitRawError, run_git
from ..memory_commit import MemoryCommitter
from ..protocol import WorkerStep
from ..session import Session
from ..worktree import SessionWorktree, WorktreeError
from .boundary import PathGuard
from .delegate_child import TranscriptRecorder
from .dispatch import ToolDispatcher
from .results import HandlerRefusal
from .shell import sanitized_env


def _wanted(value: object) -> bool:
    """The model's ``worktree`` argument; a string ``"true"`` counts too."""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


#: The way out of a worktree refusal when the call asked for one.
_ASKED = "Delegate without worktree to let it work in your files."
#: And when a workflow gave it one (TD-5195): its writers run side by side.
_FORCED = (
    "Writers that run side by side each need their own worktree; run this task "
    "on its own, or with delegate."
)


async def open_child_tree(
    parent: Session, wanted: object, *, forced: bool = False
) -> SessionWorktree | None:
    """The child's worktree when the call asked for one, or *forced* it.

    Refused rather than quietly sharing the parent's files: the model
    asked for isolation, and can ask again without it.
    """
    if not forced and not _wanted(wanted):
        return None
    try:
        from ..worktree_session import open_child  # TD-5161: it imports session -> tools

        async with _repo_lock(await _repo_key(parent.workspace_path)):
            return await open_child(parent, uuid.uuid4().hex)
    except WorktreeError as e:
        raise HandlerRefusal(
            "delegate_worktree",
            f"Refused: the child could not get its own worktree ({e}). "
            f"{_FORCED if forced and not _wanted(wanted) else _ASKED}",
        ) from e


async def close_child_tree(
    wt: SessionWorktree | None, task: str, steps: list[WorkerStep] | None
) -> str:
    """Settle the child's worktree; the note goes on the transcript and the summary."""
    if wt is None:
        return ""
    from ..worktree_session import settle_child

    async with _repo_lock(wt.repo_top):
        note = await settle_child(wt, task)
    TranscriptRecorder(steps).add("note", note)
    return note


#: How much of a settled child's diff a reviewer is shown (TD-5195).
DIFF_CHAR_CAP = 12_000


async def branch_diff(wt: SessionWorktree) -> str | None:
    """The changes a settled child left on its branch, capped.

    ``None`` when it left no branch (it changed nothing) or git cannot say.
    The worktree folder is gone by now, so this diff is the only way a
    reviewer sees what the child wrote.
    """
    env = sanitized_env()
    top = Path(wt.repo_top)
    try:
        rc, _out, _err = await run_git(
            top, "rev-parse", "--verify", "-q", f"refs/heads/{wt.branch}", env=env
        )
        if rc != 0:
            return None
        rc, out, _err = await run_git(
            top, "diff", "--no-color", "--no-ext-diff", wt.base, wt.branch, env=env
        )
    except (GitRawError, OSError, TimeoutError):
        return None
    if rc != 0:
        return None
    text = out.decode("utf-8", errors="replace")
    if len(text) > DIFF_CHAR_CAP:
        text = f"{text[:DIFF_CHAR_CAP]}\n… [diff cut at {DIFF_CHAR_CAP} characters]"
    return text


# One lock per repository and event loop: a lock belongs to the loop it
# first waited on, and a test suite runs many loops.
_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)


def _repo_lock(key: str) -> asyncio.Lock:
    locks = _LOCKS.setdefault(asyncio.get_running_loop(), {})
    lock = locks.get(key)
    if lock is None:
        lock = locks[key] = asyncio.Lock()
    return lock


async def _repo_key(workspace: str) -> str:
    """The repository top *workspace* is in, as ``SessionWorktree.repo_top`` names it."""
    try:
        rc, out, _err = await run_git(
            Path(workspace), "rev-parse", "--show-toplevel", env=sanitized_env()
        )
    except (GitRawError, OSError, TimeoutError):
        return workspace
    top = os.fsdecode(out).strip()
    if rc != 0 or not top:
        return workspace
    return str(await asyncio.to_thread(Path(top).resolve))


def worktree_brief(parent_root: str, wt: SessionWorktree) -> str:
    """The line that tells the child where it works, for paths the parent named."""
    return (
        f"You work in your own git worktree, {wt.root}: the workspace as of its last "
        f"commit. A path under {parent_root} is the same path relative to {wt.root}; "
        "use the relative form."
    )


def bind_child_root(parent: Session, child: Session, dispatcher: ToolDispatcher) -> None:
    """Point the child's root-bound tool pieces at its worktree.

    The checkpointer is dropped: the child's branch is its record, and the
    parent's checkpointer would commit the child's paths from the wrong
    root. The ledger stays the parent's, so the decisions log is one file.
    """
    from ..loop import session_boundary

    # The turn's ``tst run`` rules guard the child's reads as the parent's.
    child.run_permissions = parent.run_permissions
    # Plan mode is the parent's switch: the child has no router of its own,
    # so a boundary built from the child alone never saw plan mode turned on
    # while it ran (TD-5195), and a worktree writer kept writing.
    boundary = replace(
        session_boundary(child),
        plan_mode=lambda: parent.router is not None and parent.router.plan_mode,
    )
    root = Path(child.workspace_path)
    dispatcher.path_guard = PathGuard(boundary)
    if dispatcher.classifier is not None:
        dispatcher.classifier = dispatcher.classifier.for_boundary(boundary)
    dispatcher.checkpointer = None
    if dispatcher.memory_committer is not None:
        dispatcher.memory_committer = MemoryCommitter(root)
    dispatcher.workspace = root
