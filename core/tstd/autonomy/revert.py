"""Drift revert and consecutive-drift streak (TD-4202, spec §12.6).

A supervisor check that reports ``drift_detected`` restores the working
tree and the ``tst/auto/<slug>`` tip to the last *passing* checkpoint,
logs why, and pins the next turn to brain. Two detections in a row stop
the run with ``breaker:drift`` so the existing wake-up path notifies.

Interactive verify (TD-4204) does not import or call this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ..logging import get_logger
from .checkpoint import auto_branch, auto_ref

if TYPE_CHECKING:
    from ..session import Session

log = get_logger("tstd.autonomy.revert")

DRIFT_STOP = "breaker:drift"
_GIT_TIMEOUT = 30.0
_PROTECTED_REFS = frozenset({"refs/heads/main", "refs/heads/master", "HEAD"})


@dataclass(frozen=True)
class RevertOutcome:
    """Result of pointing the auto-branch and worktree at last good."""

    status: Literal["reverted", "no_checkpoint", "error"]
    good_sha: str | None = None
    branch: str | None = None
    reason: str = ""
    restored: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()


async def apply_drift_result(session: Session, result: object | None) -> bool:
    """Apply this turn's drift check. ``True`` means the run must stop.

    ``result is None`` means no check ran this turn — streak and last-good
    are left alone. A clean check resets the streak and records the auto
    tip as last good. Drift increments the streak, reverts when a last
    good SHA exists, and re-plans on brain unless the streak reaches 2.
    """
    if result is None:
        return False
    if not bool(getattr(result, "drift_detected", False)):
        session.autonomy_drift_streak = 0
        session.autonomy_last_good_sha = await _tip_sha(session)
        return False

    outcome = await revert_to_last_good(session)
    session.autonomy_drift_streak += 1
    if outcome.status == "reverted" and outcome.good_sha is not None:
        session.autonomy_last_check_sha = outcome.good_sha
    log.info(
        "autonomy drift revert",
        extra={
            "extra_fields": {
                "session_id": session.id,
                "streak": session.autonomy_drift_streak,
                "status": outcome.status,
                "good_sha": outcome.good_sha,
                "branch": outcome.branch,
                "reason": outcome.reason or _drift_reason(result),
                "serves_objective": getattr(result, "serves_objective", None),
                "class_a_drifted": getattr(result, "class_a_drifted", None),
                "progress_real": getattr(result, "progress_real", None),
            }
        },
    )
    if session.autonomy_drift_streak >= 2:
        session.autonomy_stop_reason = DRIFT_STOP
        return True
    _force_brain(session)
    return False


async def revert_to_last_good(session: Session) -> RevertOutcome:
    """Restore the auto-branch and its drifted paths to last good.

    No last-good SHA is a no-op (fail closed: do not invent a tree),
    still counted as a drift by the caller.
    """
    good = session.autonomy_last_good_sha
    if not good:
        return RevertOutcome(
            status="no_checkpoint",
            reason="no last good checkpoint; no-op revert",
        )
    charter = session.charter
    if charter is None:
        return RevertOutcome(status="error", good_sha=good, reason="no charter")
    try:
        branch = auto_branch(charter.slug)
    except ValueError as e:
        return RevertOutcome(status="error", good_sha=good, reason=str(e))
    try:
        return await restore_auto_branch(Path(session.workspace_path), good_sha=good, branch=branch)
    except Exception as e:
        log.exception(
            "drift revert failed",
            extra={"extra_fields": {"session_id": session.id, "error": str(e)}},
        )
        return RevertOutcome(
            status="error",
            good_sha=good,
            branch=branch,
            reason=str(e),
        )


async def restore_auto_branch(
    workspace: Path,
    *,
    good_sha: str,
    branch: str,
) -> RevertOutcome:
    """Move ``tst/auto/<slug>`` to *good_sha* and restore drifted paths.

    Uses a temporary index + ``checkout-index`` so HEAD, the user's
    index, and ``refs/heads/main`` are never written.
    """
    ref = auto_ref(branch)
    if ref in _PROTECTED_REFS:
        raise ValueError(f"refusing to write protected ref: {ref!r}")
    tip = await _rev_parse(workspace, ref)
    restore, delete = await _drifted_paths(workspace, good_sha, tip)
    await _restore_paths(workspace, good_sha, restore)
    await _delete_paths(workspace, delete)
    rc, _, err = await _git(workspace, "update-ref", ref, good_sha)
    if rc != 0:
        return RevertOutcome(
            status="error",
            good_sha=good_sha,
            branch=branch,
            reason=err.strip() or "git update-ref failed",
            restored=tuple(restore),
            deleted=tuple(delete),
        )
    return RevertOutcome(
        status="reverted",
        good_sha=good_sha,
        branch=branch,
        reason="reverted to last good checkpoint",
        restored=tuple(restore),
        deleted=tuple(delete),
    )


def _force_brain(session: Session) -> None:
    router = session.router
    if router is None:
        return
    router.set_tier("brain")


def _drift_reason(result: object) -> str:
    parts: list[str] = []
    if getattr(result, "serves_objective", True) is False:
        parts.append("does not serve the objective")
    if getattr(result, "class_a_drifted", False) is True:
        parts.append("Class A drifted from intent")
    if getattr(result, "progress_real", True) is False:
        parts.append("progress is not real")
    if getattr(result, "unparseable", False) is True:
        parts.append("unparseable validator answer")
    return "; ".join(parts) or "drift detected"


async def _tip_sha(session: Session) -> str | None:
    if session.autonomy_last_check_sha:
        return session.autonomy_last_check_sha
    charter = session.charter
    if charter is None:
        return None
    try:
        ref = auto_ref(auto_branch(charter.slug))
    except ValueError:
        return None
    return await _rev_parse(Path(session.workspace_path), ref)


async def _drifted_paths(
    workspace: Path, good_sha: str, tip: str | None
) -> tuple[list[str], list[str]]:
    """Paths the auto-branch moved since *good_sha*. User-only dirt is left."""
    if tip is None or tip == good_sha:
        return [], []
    restore: list[str] = []
    delete: list[str] = []
    for code, path in await _name_status(workspace, good_sha, tip):
        if _safe_rel(workspace, path) is None:
            continue
        if code == "A":
            delete.append(path)
        else:
            restore.append(path)
    return restore, delete


async def _restore_paths(workspace: Path, good_sha: str, paths: list[str]) -> None:
    if not paths:
        return
    index_fd, index_path = tempfile.mkstemp(prefix="tstd-revert-index-")
    os.close(index_fd)
    os.unlink(index_path)
    env = _git_env({"GIT_INDEX_FILE": index_path})
    try:
        rc, _, err = await _git(workspace, "read-tree", good_sha, env=env)
        if rc != 0:
            raise RuntimeError(f"git read-tree failed: {err.strip()}")
        for i in range(0, len(paths), 64):
            chunk = paths[i : i + 64]
            rc, _, err = await _git(workspace, "checkout-index", "-f", "--", *chunk, env=env)
            if rc != 0:
                raise RuntimeError(f"git checkout-index failed: {err.strip()}")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(index_path)


async def _delete_paths(workspace: Path, paths: list[str]) -> None:
    for rel in paths:
        target = _safe_rel(workspace, rel)
        if target is None or not target.is_file():
            continue
        try:
            await asyncio.to_thread(target.unlink)
        except OSError:
            log.warning("drift revert could not delete %s", rel)


def _safe_rel(workspace: Path, rel: str) -> Path | None:
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return None
    root = workspace.resolve()
    target = (workspace / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


async def _name_status(workspace: Path, a: str, b: str | None = None) -> list[tuple[str, str]]:
    args = ["diff", "--name-status", "--no-renames", a]
    if b is not None:
        args.append(b)
    rc, out, _ = await _git(workspace, *args)
    if rc != 0:
        return []
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        rows.append((parts[0][:1], parts[-1]))
    return rows


async def _rev_parse(workspace: Path, ref: str) -> str | None:
    if ref in _PROTECTED_REFS:
        return None
    rc, out, _ = await _git(workspace, "rev-parse", "--verify", "-q", ref)
    sha = out.strip()
    return sha if rc == 0 and sha else None


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    from ..tools.shell import sanitized_env

    env = sanitized_env()
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR",
    ):
        env.pop(var, None)
    if extra:
        env.update(extra)
    return env


async def _git(
    workspace: Path, *args: str, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    if args and args[0] == "update-ref" and len(args) >= 2 and args[1] in _PROTECTED_REFS:
        return 1, "", f"refusing to write protected ref: {args[1]}"
    if env is None:
        env = _git_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(workspace),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as e:
        return 1, "", str(e)
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, "", "git timed out"
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, out_b.decode("utf-8", errors="replace"), err_b.decode("utf-8", errors="replace")
