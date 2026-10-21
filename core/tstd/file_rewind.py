"""Restore files to a user turn from the session branch (TD-5125).

The session branch ``tst/session/<id>`` already holds every agent write
(TD-705). Each commit records the session log's seq when it was made
(``Log-Seq``), so a user turn's ``user_turn`` seq splits the branch: the
commits after it are what the agent did since that message.

For each file an agent (or an earlier restore) commit changed since then,
the target is the file as it stood just before the first such commit:
the branch copy, or the person's own copy when a pre-image commit recorded
one (:mod:`tstd.autonomy.checkpoint_message`). A file is *hand-edited* when
something other than the agent changed it after the agent first did: a
pre-image commit between the agent's writes, or the file on disk differing
from the branch tip now. Those are shown and left alone unless the person
ticks them.

Git plumbing only, as the checkpointer: temp indexes, ``diff-tree``,
``commit-tree``, ``update-ref`` of ``tst/session/<id>``. HEAD, the person's
index and every other ref are never written. Files are written by
:func:`tstd.git_restore.restore_blobs`, so no filter driver runs (TD-5157).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from .autonomy.checkpoint import agent_identity, session_branch
from .autonomy.checkpoint_message import (
    restore_message,
)
from .file_rewind_log import RewindError, _branch_tip, _Commit, _touched_since
from .git_raw import GitRawError, checked_git, repo_top, run_git, temp_index
from .git_restore import restore_blobs
from .git_stage import stage_paths

__all__ = ["RewindError", "apply_rewind", "plan_rewind"]

Change = Literal["modify", "delete", "recreate"]

#: The shown diff stops here; the file list is always whole.
MAX_DIFF_CHARS = 200_000
_LITERAL = "--literal-pathspecs"


@dataclass(frozen=True)
class RewindFile:
    """One file a restore would change.

    ``change`` is what restoring does to it: ``modify`` its content,
    ``delete`` it (the agent made it), or ``recreate`` it (the agent
    deleted it). ``hand_edited`` files are left alone unless ticked.
    """

    path: str
    change: Change
    hand_edited: bool


@dataclass(frozen=True)
class RewindPlan:
    """What "Restore files to here" would do, as of ``tip`` and the disk now."""

    plan_id: str
    files: tuple[RewindFile, ...]
    diff: str
    diff_truncated: bool
    tip: str | None = None
    target_tree: str | None = None


@dataclass(frozen=True)
class RewindResult:
    """Files written, removed, left alone, and left as git-lfs pointers."""

    restored: tuple[str, ...]
    removed: tuple[str, ...]
    left_alone: tuple[str, ...]
    pointers: tuple[str, ...]


EMPTY_PLAN = RewindPlan(plan_id="none", files=(), diff="", diff_truncated=False)


def _git_env() -> dict[str, str]:
    from .tools.shell import sanitized_env

    env = sanitized_env()
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR",
    ):
        env.pop(var, None)
    return env


async def plan_rewind(workspace: Path, session_id: str, since_seq: int) -> RewindPlan:
    """The files the agent changed after log seq *since_seq*, and their diff.

    Nothing on disk or in git changes, beyond loose objects for the files
    hashed to compare them.
    """
    env = _git_env()
    top = await _top(workspace, env)
    tip = await _branch_tip(top, session_id, env)
    if tip is None:
        return EMPTY_PLAN
    touched, flagged = await _touched_since(top, tip, session_id, since_seq, env)
    root = await asyncio.to_thread(lambda: os.path.abspath(workspace.resolve()))
    touched = {p: c for p, c in touched.items() if _inside(top, p, root)}
    if not touched:
        return EMPTY_PLAN
    paths = sorted(touched)
    target = await _target_tree(top, tip, touched, env)
    current = await _disk_tree(top, tip, paths, env)
    tip_tree = (await checked_git(top, "rev-parse", f"{tip}^{{tree}}", env=env)).decode().strip()
    for path, _code in await _raw_diff(top, tip_tree, current, env):
        flagged.add(_owner(path, touched) or path)
    rows = await _raw_diff(top, target, current, env)
    files = tuple(
        RewindFile(
            path=path,
            change=_change(code),
            hand_edited=(_owner(path, touched) or path) in flagged,
        )
        for path, code in rows
    )
    if not files:
        return EMPTY_PLAN
    diff, truncated = await _patch(top, target, current, env)
    digest = hashlib.sha256(
        "\0".join([tip, target, current, *sorted(f.path for f in files if f.hand_edited)]).encode()
    ).hexdigest()[:20]
    return RewindPlan(
        plan_id=digest,
        files=files,
        diff=diff,
        diff_truncated=truncated,
        tip=tip,
        target_tree=target,
    )


async def apply_rewind(
    workspace: Path,
    session_id: str,
    since_seq: int,
    *,
    turn_id: str,
    plan_id: str,
    include_hand_edited: Sequence[str],
    log_seq: int,
) -> RewindResult:
    """Restore the plan the person saw, and record it on the session branch.

    The plan is made again first: when the branch or a file changed since
    the preview, ``plan_id`` no longer matches and nothing is written.
    """
    plan = await plan_rewind(workspace, session_id, since_seq)
    if plan.plan_id != plan_id or plan.tip is None or plan.target_tree is None:
        raise RewindError(
            "restore_stale",
            "The files changed since the preview. Look at the new preview before restoring.",
        )
    ticked = set(include_hand_edited)
    chosen = [f for f in plan.files if not f.hand_edited or f.path in ticked]
    left = tuple(f.path for f in plan.files if f.hand_edited and f.path not in ticked)
    if not chosen:
        return RewindResult(restored=(), removed=(), left_alone=left, pointers=())
    env = _git_env()
    top = await _top(workspace, env)
    written = await restore_blobs(
        top,
        plan.target_tree,
        [f.path for f in chosen if f.change != "delete"],
        remove=[f.path for f in chosen if f.change == "delete"],
        env=env,
    )
    await _record_restore(
        top,
        session_id,
        plan.tip,
        [f.path for f in chosen],
        turn_id=turn_id,
        log_seq=log_seq,
        env=env,
    )
    return RewindResult(
        restored=written.restored,
        removed=written.removed,
        left_alone=left,
        pointers=written.pointers,
    )


# ── Reading the branch ───────────────────────────────────────────────────


async def _top(workspace: Path, env: Mapping[str, str]) -> Path:
    rc, out, _ = await run_git(workspace, "rev-parse", "--is-inside-work-tree", env=env)
    if rc != 0 or out.strip() != b"true":
        raise RewindError(
            "rewind_unavailable",
            "This workspace is not a git repository, so there are no checkpoints to restore.",
        )
    return await repo_top(workspace, env)


# ── Building the two trees ───────────────────────────────────────────────


async def _target_tree(
    top: Path, tip: str, touched: Mapping[str, _Commit], env: Mapping[str, str]
) -> str:
    """The tip's tree with each touched file as it was before the agent's first change."""
    by_parent: dict[str | None, list[str]] = {}
    for path, commit in touched.items():
        by_parent.setdefault(commit.parent, []).append(path)
    entries: dict[str, tuple[str, str]] = {}
    for parent, paths in by_parent.items():
        if parent is not None:
            entries.update(await _ls_tree(top, parent, paths, env))
    zero = "0" * len(tip)
    lines = [f"0 {zero}\t".encode() + os.fsencode(p) + b"\0" for p in touched if p not in entries]
    lines += [f"{m} {o}\t".encode() + os.fsencode(p) + b"\0" for p, (m, o) in entries.items()]
    with temp_index("tstd-rewind-target-") as index:
        index_env = {**env, "GIT_INDEX_FILE": index}
        await checked_git(top, "read-tree", tip, env=index_env)
        await checked_git(
            top, "update-index", "-z", "--index-info", env=index_env, stdin=b"".join(lines)
        )
        return (await checked_git(top, "write-tree", env=index_env)).decode().strip()


async def _ls_tree(
    top: Path, commit: str, paths: Sequence[str], env: Mapping[str, str]
) -> dict[str, tuple[str, str]]:
    wanted = set(paths)
    found: dict[str, tuple[str, str]] = {}
    for i in range(0, len(paths), 256):
        out = await checked_git(
            top,
            _LITERAL,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            commit,
            "--",
            *paths[i : i + 256],
            env=env,
        )
        for record in out.split(b"\0"):
            meta, tab, raw = record.partition(b"\t")
            path = os.fsdecode(raw)
            if tab and path in wanted:
                mode, _kind, oid = meta.decode().split(" ")
                found[path] = (mode, oid)
    return found


async def _disk_tree(top: Path, tip: str, paths: Sequence[str], env: Mapping[str, str]) -> str:
    """The tip's tree with the touched files as they are on disk now."""
    with temp_index("tstd-rewind-disk-") as index:
        index_env = {**env, "GIT_INDEX_FILE": index}
        await checked_git(top, "read-tree", tip, env=index_env)
        try:
            await stage_paths(top, [top / p for p in paths], env=index_env, missing_ok=True)
        except GitRawError as e:
            raise RewindError(
                "rewind_failed", f"Couldn't read the files as they are now: {e}"
            ) from e
        return (await checked_git(top, "write-tree", env=index_env)).decode().strip()


async def _raw_diff(top: Path, a: str, b: str, env: Mapping[str, str]) -> list[tuple[str, str]]:
    """``(path, status letter)`` for each file that differs between trees *a* and *b*."""
    out = await checked_git(top, "diff-tree", "-r", "-z", "--no-renames", a, b, env=env)
    fields = out.split(b"\0")
    rows: list[tuple[str, str]] = []
    for meta, raw in zip(fields[0::2], fields[1::2], strict=False):
        if meta.startswith(b":") and raw:
            rows.append((os.fsdecode(raw), meta.decode().split(" ")[-1][:1]))
    return rows


async def _patch(top: Path, target: str, current: str, env: Mapping[str, str]) -> tuple[str, bool]:
    """The changes a restore undoes: from each file's target to how it is now."""
    out = await checked_git(
        top,
        "diff-tree",
        "-p",
        "-r",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        target,
        current,
        env=env,
    )
    text = out.decode("utf-8", errors="replace")
    if len(text) <= MAX_DIFF_CHARS:
        return text, False
    cut = text.rfind("\n", 0, MAX_DIFF_CHARS)
    return text[: cut if cut > 0 else MAX_DIFF_CHARS], True


def _change(code: str) -> Change:
    if code == "A":
        return "delete"  # on disk now, not in the target
    if code == "D":
        return "recreate"
    return "modify"


def _owner(path: str, touched: Mapping[str, object]) -> str | None:
    """*path*, or the touched file a folder now stands in for."""
    probe = PurePosixPath(path)
    for candidate in (probe, *probe.parents):
        if str(candidate) in touched:
            return str(candidate)
    return None


def _inside(top: Path, rel: str, root: str) -> bool:
    """The agent only writes inside its workspace (*root*); never restore past it."""
    if not rel or rel.startswith("/") or ".." in PurePosixPath(rel).parts:
        return False
    full = os.path.abspath(top / rel)
    return full == root or full.startswith(root.rstrip(os.sep) + os.sep)


# ── Recording the restore ────────────────────────────────────────────────


async def _record_restore(
    top: Path,
    session_id: str,
    tip: str,
    paths: Sequence[str],
    *,
    turn_id: str,
    log_seq: int,
    env: Mapping[str, str],
) -> None:
    """Commit the restored files, as on disk now, on top of *tip*.

    Without it the next restore would read them as hand-edited. The ref
    moves only if it still names *tip*.
    """
    ident = {**env, **agent_identity()}
    with temp_index("tstd-rewind-record-") as index:
        index_env = {**ident, "GIT_INDEX_FILE": index}
        await checked_git(top, "read-tree", tip, env=index_env)
        await stage_paths(top, [top / p for p in paths], env=index_env, missing_ok=True)
        tree = (await checked_git(top, "write-tree", env=index_env)).decode().strip()
    message = restore_message(session_id=session_id, turn_id=turn_id, rels=paths, log_seq=log_seq)
    commit = (
        (await checked_git(top, "commit-tree", tree, "-p", tip, "-m", message, env=ident))
        .decode()
        .strip()
    )
    ref = f"refs/heads/{session_branch(session_id)}"
    await checked_git(top, "update-ref", ref, commit, tip, env=env)
