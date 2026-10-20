"""A delegate child in its own git worktree (TD-5126).

``delegate`` with ``worktree: true`` gives the child a worktree on a new
branch from the parent's last commit, so its edits cannot collide with
the parent's. The child's tools are rebound to that folder: its paths are
guarded and judged against its own root, and memory commits land on its
branch. When it finishes, its changes are committed on the branch and the
folder is removed; the parent reviews and merges the branch through the
shell, under the classifier like any other command. A child that changed
nothing leaves no branch behind.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from ..memory_commit import MemoryCommitter
from ..protocol import WorkerStep
from ..session import Session
from ..worktree import SessionWorktree, WorktreeError
from ..worktree_session import open_child, settle_child
from .boundary import PathGuard
from .delegate_child import TranscriptRecorder
from .dispatch import ToolDispatcher
from .results import HandlerRefusal


def _wanted(value: object) -> bool:
    """The model's ``worktree`` argument; a string ``"true"`` counts too."""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


async def open_child_tree(parent: Session, wanted: object) -> SessionWorktree | None:
    """The child's worktree when the call asked for one.

    Refused rather than quietly sharing the parent's files: the model
    asked for isolation, and can ask again without it.
    """
    if not _wanted(wanted):
        return None
    try:
        return await open_child(parent, uuid.uuid4().hex)
    except WorktreeError as e:
        raise HandlerRefusal(
            "delegate_worktree",
            f"Refused: the child could not get its own worktree ({e}). "
            "Delegate without worktree to let it work in your files.",
        ) from e


async def close_child_tree(
    wt: SessionWorktree | None, task: str, steps: list[WorkerStep] | None
) -> str:
    """Settle the child's worktree; the note goes on the transcript and the summary."""
    if wt is None:
        return ""
    note = await settle_child(wt, task)
    TranscriptRecorder(steps).add("note", note)
    return note


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
    boundary = session_boundary(child)
    root = Path(child.workspace_path)
    dispatcher.path_guard = PathGuard(boundary)
    if dispatcher.classifier is not None:
        dispatcher.classifier = dispatcher.classifier.for_boundary(boundary)
    dispatcher.checkpointer = None
    if dispatcher.memory_committer is not None:
        dispatcher.memory_committer = MemoryCommitter(root)
    dispatcher.workspace = root
