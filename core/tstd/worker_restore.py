"""After a restart: background workers that were running are interrupted (TD-5299).

A worker is a task in the daemon and dies with it. On revive the last
``worker_jobs`` list comes back with every live worker marked
``interrupted``. A worker that had its own worktree has what was in the
folder committed on its branch and the folder removed, as a settled child's
would be. Nothing is re-run: that would spend without the person asking.
The next turn's first model call is told.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from .git_raw import GitRawError, run_git
from .logging import get_logger
from .protocol import DaemonEvent, WorkerJobs
from .session import Session
from .tools.shell import sanitized_env
from .worktree import commit_all

log = get_logger("tstd.worker_restore")


async def _settle_folder(path: Path, task: str) -> str:
    env = sanitized_env()
    if not await asyncio.to_thread(path.is_dir):
        return ""
    try:
        line = task.strip().splitlines()[0][:72] if task.strip() else "delegated task"
        await commit_all(path, f"{line}\n\nTST Desk delegate, interrupted by a restart.", env)
        rc, out, _err = await run_git(
            path, "rev-parse", "--path-format=absolute", "--git-common-dir", env=env
        )
        if rc != 0:
            return f"The worker's worktree at {path} could not be tidied."
        top = Path(out.decode().strip()).parent
        await run_git(top, "worktree", "remove", "--force", "--force", str(path), env=env)
    except (GitRawError, OSError, TimeoutError) as e:
        return f"The worker's worktree at {path} could not be tidied: {e}"
    return "What it had changed is committed on its branch; the folder was removed."


async def restore_workers(session: Session, events: Sequence[DaemonEvent]) -> int:
    """Mark the old daemon's live workers interrupted. Returns how many."""
    last = next((e for e in reversed(events) if isinstance(e, WorkerJobs)), None)
    if last is None:
        return 0
    cut = session.workers.restore(last.workers)
    for job in cut:
        if job.worktree_path:
            job.summary = await _settle_folder(Path(job.worktree_path), job.task)
            job.worktree_path = None
    if cut:
        await session.workers.changed(cut[0])
        log.info(
            "background workers interrupted by a restart",
            extra={"extra_fields": {"session_id": session.id, "workers": len(cut)}},
        )
    return len(cut)
