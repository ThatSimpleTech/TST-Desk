"""Commit accepted memory writes on HEAD (TD-2104).

Checkpointer never touches HEAD — that is the session-branch undo stack.
Memory commits are the opposite: they land on the workspace repo as
``tst: memory update`` so ``git revert`` restores the previous bytes.

The write has already landed when this runs. A missing git repo or a
failed commit never fails the write. The no-git notice is one-time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .autonomy.checkpoint import Notice
from .logging import get_logger
from .memory_store import path_is_memory_file

log = get_logger("tstd.memory_commit")

MEMORY_COMMIT_SUBJECT = "tst: memory update"
MEMORY_NO_GIT = "memory_no_git"

_AGENT_NAME = "TST Desk"
_AGENT_EMAIL = "tstdesk@localhost"
_GIT_TIMEOUT = 30.0


@dataclass(frozen=True)
class MemoryCommitOutcome:
    """Result of a memory-commit attempt. Never raised into the write."""

    status: Literal["committed", "disabled", "skipped", "error"]
    commit: str | None = None
    notice: Notice | None = None
    reason: str = ""


class MemoryCommitter:
    """Commits memory paths on HEAD. One instance per session."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._probed = False
        self._disabled = False
        self._disabled_reason = ""
        self._notified: set[str] = set()

    async def commit(self, paths: Sequence[Path]) -> MemoryCommitOutcome:
        """Commit memory files on HEAD. Non-memory paths are ignored."""
        memory_paths = [p for p in paths if path_is_memory_file(p, self._workspace)]
        if not memory_paths:
            return MemoryCommitOutcome(status="skipped")
        try:
            return await self._commit_inner(memory_paths)
        except Exception as e:
            log.warning(
                "memory commit failed",
                extra={"extra_fields": {"workspace": str(self._workspace), "error": str(e)}},
            )
            return MemoryCommitOutcome(status="error", reason=str(e))

    async def _commit_inner(self, paths: Sequence[Path]) -> MemoryCommitOutcome:
        if self._disabled:
            return MemoryCommitOutcome(status="disabled", reason=self._disabled_reason)
        if not self._probed:
            await self._probe()
            if self._disabled:
                return MemoryCommitOutcome(
                    status="disabled",
                    notice=self._notice_once(MEMORY_NO_GIT, self._disabled_reason),
                    reason=self._disabled_reason,
                )

        rels = [self._rel(p) for p in paths]
        env = self._identity_env()
        for rel in rels:
            rc, _, err = await self._git("add", "--", rel, env=env)
            if rc != 0:
                return MemoryCommitOutcome(
                    status="error",
                    reason=err.strip() or f"git add failed for {rel}",
                )
        rc, out, err = await self._git("commit", "-m", MEMORY_COMMIT_SUBJECT, "--", *rels, env=env)
        if rc != 0:
            return MemoryCommitOutcome(
                status="error",
                reason=(err or out).strip() or "git commit failed",
            )
        sha_rc, sha, _ = await self._git("rev-parse", "HEAD")
        return MemoryCommitOutcome(
            status="committed",
            commit=sha.strip() if sha_rc == 0 else None,
        )

    async def _probe(self) -> None:
        self._probed = True
        try:
            rc, out, _ = await self._git("rev-parse", "--is-inside-work-tree")
        except (OSError, TimeoutError):
            self._disabled = True
            self._disabled_reason = (
                "Memory write landed; this workspace is not a git repository "
                "so there is no commit to revert."
            )
            return
        if rc != 0 or out.strip() != "true":
            self._disabled = True
            self._disabled_reason = (
                "Memory write landed; this workspace is not a git repository "
                "so there is no commit to revert."
            )

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self._workspace).as_posix()
        except ValueError:
            return str(path)

    def _notice_once(self, code: str, message: str) -> Notice | None:
        if code in self._notified:
            return None
        self._notified.add(code)
        return Notice(code=code, message=message)

    @staticmethod
    def _identity_env() -> dict[str, str]:
        from .tools.shell import sanitized_env  # local import: no cycle

        return {
            **sanitized_env(),
            "GIT_AUTHOR_NAME": _AGENT_NAME,
            "GIT_AUTHOR_EMAIL": _AGENT_EMAIL,
            "GIT_COMMITTER_NAME": _AGENT_NAME,
            "GIT_COMMITTER_EMAIL": _AGENT_EMAIL,
        }

    async def _git(self, *args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        """Run ``git -C <workspace> <args>``; returns (rc, stdout, stderr).

        The child inherits the shell tool's ``sanitized_env()`` (TD-4816);
        the agent identity rides on top of it.
        """
        if env is None:
            from .tools.shell import sanitized_env

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
