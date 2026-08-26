"""Checkpoint commits (TD-705) — the session branch as undo stack.

Every successful mutating tool call commits a snapshot of the written
paths to a dedicated session branch ``tst/session/<id>``.  The branch is
the undo stack, the audit trail, and the review surface (spec §12.8).

Safety model — git plumbing only:

- The only ref ever written is ``refs/heads/tst/session/<id>`` via
  ``git update-ref``.  HEAD, the user's index, and the working tree are
  never touched by any command this module runs, which is the structural
  guarantee that pre-existing uncommitted user changes are never
  clobbered.
- Each checkpoint's tree is built from the previous checkpoint's tree
  (or HEAD's tree for the first one) plus the agent-written paths, using
  a temporary index file.  Dirty working-tree changes outside the
  written paths are neither captured nor disturbed.
- Checkpoints respect ``.gitignore`` (plain ``git add``, never ``-f``),
  keeping runtime state (§8) out of the undo stack.
- Git children inherit the shell tool's ``sanitized_env()`` — no API
  keys, tokens, or keychain material reach git (TD-4816).

Degradation (informed once per session, never fatal):

- non-git workspace or missing git binary → feature disabled;
- dirty baseline at first checkpoint → reported, checkpointing continues;
- rebase in progress → checkpoint skipped until the rebase ends;
- unexpected git failure → error outcome; the write that triggered the
  checkpoint is never failed by checkpointing.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..logging import get_logger
from .classifier import DecisionClass

log = get_logger("tstd.checkpoint")

SESSION_BRANCH_PREFIX = "tst/session/"
AUTO_BRANCH_PREFIX = "tst/auto/"

# Start-gate copy (TD-4102). Interactive sessions still degrade (TD-705).
NO_GIT_FOR_AUTONOMY = (
    "Autonomous runs need a git repository so every iteration can commit "
    "on tst/auto/<charter-slug>. This workspace is not one."
)
GIT_MISSING_FOR_AUTONOMY = (
    "Autonomous runs need git so every iteration can commit on tst/auto/<charter-slug>."
)

# Notice codes surfaced to the user via the ``checkpoint_notice`` event.
NO_GIT = "no_git"
DIRTY_BASELINE = "dirty_baseline"
REBASE_IN_PROGRESS = "rebase_in_progress"
GIT_ERROR = "git_error"

# Fixed agent identity: checkpoints must never depend on the user's git
# config being present, and they should be visibly agent-authored.
_AGENT_NAME = "TST Desk"
_AGENT_EMAIL = "tstdesk@localhost"

_GIT_TIMEOUT = 30.0


@dataclass(frozen=True)
class Notice:
    """A one-time user-facing notice produced by a checkpoint attempt."""

    code: str
    message: str


@dataclass(frozen=True)
class CheckpointOutcome:
    """The result of a checkpoint attempt.

    ``status`` is ``committed`` (snapshot on the session branch),
    ``disabled`` (no git repo — sticky for the session), ``skipped``
    (rebase in progress), or ``error`` (unexpected git failure).  Only
    ``committed`` carries a ``commit`` SHA.
    """

    status: Literal["committed", "disabled", "skipped", "error"]
    commit: str | None = None
    branch: str | None = None
    notice: Notice | None = None
    reason: str = ""


def session_branch(session_id: str) -> str:
    """Return the checkpoint branch name for *session_id*.

    Defense-in-depth for "never commits to main": the result is always
    under ``tst/session/`` and can never name a primary branch.  Raises
    ``ValueError`` for session ids that could not form such a ref.
    """
    if not session_id or session_id.startswith("-") or "/" in session_id:
        raise ValueError(f"invalid session id for checkpoint branch: {session_id!r}")
    branch = SESSION_BRANCH_PREFIX + session_id
    if not branch.startswith(SESSION_BRANCH_PREFIX) or branch.removeprefix(
        SESSION_BRANCH_PREFIX
    ) in {"main", "master"}:
        raise ValueError(f"refusing checkpoint branch name: {branch!r}")
    return branch


def auto_branch(slug: str) -> str:
    """Return the autonomy checkpoint branch for a charter *slug*.

    Always under ``tst/auto/``. Raises ``ValueError`` for slugs that
    could name a primary branch or an unsafe ref.
    """
    if not slug or slug.startswith("-") or "/" in slug:
        raise ValueError(f"invalid charter slug for checkpoint branch: {slug!r}")
    branch = AUTO_BRANCH_PREFIX + slug
    if not branch.startswith(AUTO_BRANCH_PREFIX) or branch.removeprefix(AUTO_BRANCH_PREFIX) in {
        "main",
        "master",
    }:
        raise ValueError(f"refusing checkpoint branch name: {branch!r}")
    return branch


def auto_ref(branch: str) -> str:
    """``refs/heads/tst/auto/<slug>``. Never names HEAD, main, or master.

    Revert (TD-4202) and checkpoint share this so an auto-branch move
    cannot be pointed at the user's default branch.
    """
    if not branch.startswith(AUTO_BRANCH_PREFIX):
        raise ValueError(f"autonomy ref must start with {AUTO_BRANCH_PREFIX!r}: {branch!r}")
    if branch.removeprefix(AUTO_BRANCH_PREFIX) in {"main", "master"}:
        raise ValueError(f"refusing to name a primary branch: {branch!r}")
    ref = f"refs/heads/{branch}"
    if ref in {"refs/heads/main", "refs/heads/master", "HEAD"}:
        raise ValueError(f"refusing protected ref: {ref!r}")
    return ref


async def checkpoint_start_error(workspace: str | Path) -> str | None:
    """Why an autonomous run may not checkpoint here, or ``None``.

    Interactive sessions degrade when git is missing (TD-705). An
    unattended run refuses: the branch is the undo stack (spec §12.8).
    """
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
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(workspace),
            "rev-parse",
            "--is-inside-work-tree",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        return GIT_MISSING_FOR_AUTONOMY
    except OSError:
        return GIT_MISSING_FOR_AUTONOMY
    try:
        out_b, _err_b = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return GIT_MISSING_FOR_AUTONOMY
    rc = proc.returncode if proc.returncode is not None else -1
    if rc != 0 or out_b.decode("utf-8", errors="replace").strip() != "true":
        return NO_GIT_FOR_AUTONOMY
    return None


class Checkpointer:
    """Commits agent-written paths to the session (or autonomy) branch.

    One instance per session.  Degradation state (disabled, notices
    already delivered) is tracked per instance so "informed once" holds
    for the session's lifetime.  Interactive runs use
    ``tst/session/<id>``.  Autonomous runs pass ``branch=`` from
    :func:`auto_branch` (TD-4102).
    """

    def __init__(
        self,
        workspace: Path,
        session_id: str,
        branch: str | None = None,
    ) -> None:
        self._workspace = workspace
        self._session_id = session_id
        if branch is None:
            self._branch = session_branch(session_id)
        else:
            if not branch.startswith(AUTO_BRANCH_PREFIX):
                raise ValueError(
                    f"autonomy checkpoint branch must start with {AUTO_BRANCH_PREFIX!r}"
                )
            self._branch = branch
        self._probed = False
        self._disabled = False
        self._disabled_reason = ""
        self._notified: set[str] = set()

    # ── Public entry point ────────────────────────────────────────────

    async def checkpoint(
        self,
        paths: Sequence[Path],
        *,
        tool_name: str,
        tool_call_id: str,
        decision_class: DecisionClass | None,
        decision_rule: str | None = None,
    ) -> CheckpointOutcome:
        """Commit *paths* (as they exist in the working tree) to the branch.

        Never raises for expected degradation; unexpected git failures
        become ``status="error"`` outcomes.  A write must never fail
        because its checkpoint failed.
        """
        try:
            return await self._checkpoint_inner(
                paths,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                decision_class=decision_class,
                decision_rule=decision_rule,
            )
        except Exception as e:  # degradation must not propagate to the write
            log.warning(
                "checkpoint failed",
                extra={
                    "extra_fields": {
                        "session_id": self._session_id,
                        "tool_call_id": tool_call_id,
                        "error": str(e),
                    }
                },
            )
            return CheckpointOutcome(
                status="error",
                notice=self._notice_once(GIT_ERROR, f"Checkpoint failed: {e}"),
                reason=str(e),
            )

    # ── Internals ─────────────────────────────────────────────────────

    async def _checkpoint_inner(
        self,
        paths: Sequence[Path],
        *,
        tool_name: str,
        tool_call_id: str,
        decision_class: DecisionClass | None,
        decision_rule: str | None,
    ) -> CheckpointOutcome:
        if self._disabled:
            return CheckpointOutcome(status="disabled", reason=self._disabled_reason)
        if not self._probed:
            await self._probe()
            if self._disabled:
                return CheckpointOutcome(
                    status="disabled",
                    notice=self._notice_once(NO_GIT, self._disabled_reason),
                    reason=self._disabled_reason,
                )

        if await self._rebase_in_progress():
            return CheckpointOutcome(
                status="skipped",
                notice=self._notice_once(
                    REBASE_IN_PROGRESS,
                    "Checkpointing paused: a git rebase is in progress in this "
                    "workspace. Checkpoints resume when it completes.",
                ),
                reason="rebase in progress",
            )

        dirty_notice = await self._dirty_baseline_notice()
        parent = await self._resolve_parent()
        commit = await self._build_commit(
            paths, parent, tool_name, tool_call_id, decision_class, decision_rule
        )
        return CheckpointOutcome(
            status="committed",
            commit=commit,
            branch=self._branch,
            notice=dirty_notice,
        )

    async def _probe(self) -> None:
        """One-time repo detection; disables the feature when there is none."""
        self._probed = True
        try:
            rc, out, _ = await self._git("rev-parse", "--is-inside-work-tree")
        except (OSError, TimeoutError) as e:
            self._disabled = True
            self._disabled_reason = "Checkpoints disabled: git is not available in this workspace."
            log.info("checkpoint disabled: git unavailable", extra={"error": str(e)})
            return
        if rc != 0 or out.strip() != "true":
            self._disabled = True
            self._disabled_reason = "Checkpoints disabled: this workspace is not a git repository."

    async def _dirty_baseline_notice(self) -> Notice | None:
        """Report pre-existing uncommitted changes once, at first checkpoint."""
        if DIRTY_BASELINE in self._notified:
            return None
        rc, out, _ = await self._git("status", "--porcelain")
        if rc != 0 or not out.strip():
            return None
        return self._notice_once(
            DIRTY_BASELINE,
            "This workspace has uncommitted changes. Checkpoints capture only "
            "the agent's writes on the session branch; your in-progress "
            "changes are left untouched.",
        )

    async def _rebase_in_progress(self) -> bool:
        rc, out, _ = await self._git("rev-parse", "--git-dir")
        if rc != 0:
            return False
        git_dir = Path(out.strip())
        if not git_dir.is_absolute():
            git_dir = self._workspace / git_dir
        return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()

    async def _resolve_parent(self) -> str | None:
        """Parent commit: session branch tip, else HEAD, else none (empty repo)."""
        rc, out, _ = await self._git("rev-parse", "--verify", "-q", f"refs/heads/{self._branch}")
        if rc == 0 and out.strip():
            return out.strip()
        rc, out, _ = await self._git("rev-parse", "--verify", "-q", "HEAD")
        if rc == 0 and out.strip():
            return out.strip()
        return None

    async def _build_commit(
        self,
        paths: Sequence[Path],
        parent: str | None,
        tool_name: str,
        tool_call_id: str,
        decision_class: DecisionClass | None,
        decision_rule: str | None,
    ) -> str:
        """Build the snapshot commit with a temp index; update the branch ref."""
        env = self._identity_env()
        index_fd, index_path = tempfile.mkstemp(prefix="tstd-checkpoint-index-")
        os.close(index_fd)
        # Git must create the index itself: a pre-existing zero-byte
        # file reads as a corrupt index ("smaller than expected").
        os.unlink(index_path)
        index_env = {**env, "GIT_INDEX_FILE": index_path}
        try:
            if parent is not None:
                rc, _, err = await self._git("read-tree", parent, env=index_env)
                if rc != 0:
                    raise RuntimeError(f"git read-tree failed: {err.strip()}")
            for path in paths:
                rc, _, err = await self._git("add", "--", str(path), env=index_env)
                if rc != 0:
                    raise RuntimeError(f"git add failed for {path}: {err.strip()}")
            rc, out, err = await self._git("write-tree", env=index_env)
            if rc != 0:
                raise RuntimeError(f"git write-tree failed: {err.strip()}")
            tree = out.strip()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(index_path)

        message = self._message(paths, tool_name, tool_call_id, decision_class, decision_rule)
        args = ["commit-tree", tree]
        if parent is not None:
            args += ["-p", parent]
        args += ["-m", message]
        rc, out, err = await self._git(*args, env=env)
        if rc != 0:
            raise RuntimeError(f"git commit-tree failed: {err.strip()}")
        commit = out.strip()

        rc, _, err = await self._git("update-ref", f"refs/heads/{self._branch}", commit)
        if rc != 0:
            raise RuntimeError(f"git update-ref failed: {err.strip()}")
        return commit

    def _message(
        self,
        paths: Sequence[Path],
        tool_name: str,
        tool_call_id: str,
        decision_class: DecisionClass | None,
        decision_rule: str | None,
    ) -> str:
        cls = decision_class.value if decision_class is not None else "?"
        via = decision_rule if decision_rule else "worker-classifier"
        rel = [self._rel(path) for path in paths]
        subject = f"checkpoint({cls}): {tool_name} {rel[0]}"
        body = "\n".join(
            [
                "",
                f"Session: {self._session_id}",
                f"Tool-Call: {tool_call_id}",
                f"Decision: class {cls} via {via}",
                f"Paths: {', '.join(rel)}",
            ]
        )
        return subject + body

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self._workspace))
        except ValueError:
            return str(path)

    def _notice_once(self, code: str, message: str) -> Notice | None:
        if code in self._notified:
            return None
        self._notified.add(code)
        return Notice(code=code, message=message)

    @staticmethod
    def _identity_env() -> dict[str, str]:
        from ..tools.shell import sanitized_env  # local import: no cycle

        return {
            **sanitized_env(),
            "GIT_AUTHOR_NAME": _AGENT_NAME,
            "GIT_AUTHOR_EMAIL": _AGENT_EMAIL,
            "GIT_COMMITTER_NAME": _AGENT_NAME,
            "GIT_COMMITTER_EMAIL": _AGENT_EMAIL,
        }

    async def _git(self, *args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        """Run ``git -C <workspace> <args>``; returns (rc, stdout, stderr).

        The child inherits the shell tool's ``sanitized_env()`` — no API
        keys, tokens, or keychain material reach git (TD-4816).  Callers
        layer additions (the temp index, the agent identity) on top.
        """
        if env is None:
            from ..tools.shell import sanitized_env

            env = sanitized_env()
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self._workspace),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, out_b.decode("utf-8", errors="replace"), err_b.decode("utf-8", errors="replace")
