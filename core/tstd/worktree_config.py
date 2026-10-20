"""A worktree session's workspace config and its trust (TD-5126).

When git does not track ``.tst/config.yaml``, it is the project's own
local file: a worktree holds a copy, because the wall, the policy and the
trust gate read the config from the session's working folder. The copy
follows the project's file. TST Desk saves Always allow, Revoke, a spend
cap and import approvals into the project's file, as it did before
worktrees, and then refreshes the copy; a session's start or revival
refreshes it too. A tracked config is the branch's own file: a save there
is a change the worktree carries back in a merge.

A trust decision (TD-5113) covers the bytes the card showed, wherever
they sit: the project's file and every worktree copy holding the same
bytes are decided together, so the next session neither asks again nor
skips its ``worktree.setup``.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from .boundary_config import workspace_config_path
from .workspace_trust import TrustStore, same_workspace
from .worktree import SessionWorktree

if TYPE_CHECKING:
    from .session import Session


def _sha(folder: str | Path) -> str | None:
    try:
        return hashlib.sha256(workspace_config_path(folder).read_bytes()).hexdigest()
    except OSError:
        return None


def carry_trust(project: str, root: Path, trust: TrustStore | None) -> None:
    """Record for *root* the decision made on the same bytes in *project*."""
    if trust is None:
        return
    digest = _sha(root)
    if digest is None:
        return
    state = trust.state_for(project, digest)
    if state in ("trusted", "declined"):
        trust.record(root, digest, "trusted" if state == "trusted" else "declined")


def carry_config(project: str, root: Path, trust: TrustStore | None) -> bool:
    """Copy an untracked workspace config into a new worktree, with its trust.

    Returns:
        True when git tracks the config: the checkout already had it.
    """
    src, dst = workspace_config_path(project), workspace_config_path(root)
    tracked = dst.exists()
    if not tracked and src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    carry_trust(project, root, trust)
    return tracked


def refresh_config(wt: SessionWorktree, trust: TrustStore | None) -> None:
    """Make an untracked config's copy match the project's file again.

    A tracked config is left alone, and so is a worktree folder that is
    gone. With no project file, the copy goes too: the project has no
    settings, so the worktree has none.
    """
    if wt.config_tracked or not Path(wt.path).is_dir():
        return
    src, dst = workspace_config_path(wt.project), workspace_config_path(wt.root)
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    elif dst.is_file():
        dst.unlink()
    carry_trust(wt.project, wt.root, trust)


def config_home(sess: Session) -> str:
    """The folder whose ``.tst/config.yaml`` TST Desk saves this session's settings to."""
    wt = sess.worktree
    return wt.project if wt is not None and not wt.config_tracked else sess.workspace_path


def same_config_folders(
    project: str, decided: str, sha256: str, worktrees: Iterable[SessionWorktree]
) -> list[str]:
    """Folders besides *decided* whose config is exactly the bytes *sha256* names.

    The candidates are the project folder and its worktrees' copies of it.
    """
    found: list[str] = []
    for folder in (
        project,
        *(str(wt.root) for wt in worktrees if same_workspace(wt.project, project)),
    ):
        if same_workspace(folder, decided) or any(same_workspace(folder, f) for f in found):
            continue
        if _sha(folder) == sha256:
            found.append(folder)
    return found


__all__ = [
    "carry_config",
    "carry_trust",
    "config_home",
    "refresh_config",
    "same_config_folders",
]
