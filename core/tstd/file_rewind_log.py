"""Reading a session's checkpoint branch for a restore (TD-5125).

Split out of ``file_rewind`` (TD-5161: AGENTS.md keeps modules under
about 400 lines). Each commit's trailers say which session made it,
whether it was an agent write or the person's pre-image, and the event
log seq it came after.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .autonomy.checkpoint import AGENT_EMAIL, session_branch
from .autonomy.checkpoint_message import KIND_PREIMAGE, parse_trailers
from .git_raw import checked_git, run_git

_LOG_CHUNK = 200
#: Each commit's record opens with NUL NUL RS. With ``-z`` every path is
#: NUL-terminated and never empty, so two NULs in a row only ever start a
#: record; RS alone could not frame one, since a file name may contain it.
_LOG_FORMAT = "%x00%x00%x1e%H%x1f%P%x1f%ae%x1f%B%x1f"
_LOG_RECORD = b"\0\0\x1e"


class RewindError(Exception):
    """A restore that cannot go ahead; ``code`` is the protocol error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _Commit:
    sha: str
    parent: str | None
    kind: str
    log_seq: int | None
    paths: tuple[str, ...]


async def _branch_tip(top: Path, session_id: str, env: Mapping[str, str]) -> str | None:
    ref = f"refs/heads/{session_branch(session_id)}"
    rc, out, _ = await run_git(top, "rev-parse", "--verify", "-q", ref, env=env)
    sha = out.decode().strip()
    return sha if rc == 0 and sha else None


async def _touched_since(
    top: Path, tip: str, session_id: str, since_seq: int, env: Mapping[str, str]
) -> tuple[dict[str, _Commit], set[str]]:
    """Each file's first agent or restore commit after *since_seq*, and the hand-edited ones."""
    after: list[_Commit] = []
    for commit in await _session_commits(top, tip, session_id, since_seq, env):
        if commit.log_seq is None:
            raise RewindError(
                "rewind_predates",
                "Some of this session's checkpoints are older than restore, so it can't "
                "tell which came after this message. Use the decision's git revert instead.",
            )
        if commit.log_seq <= since_seq:
            break
        after.append(commit)
    first: dict[str, _Commit] = {}
    flagged: set[str] = set()
    for commit in reversed(after):  # oldest first
        for path in commit.paths:
            if commit.kind == KIND_PREIMAGE:
                if path in first:
                    flagged.add(path)  # changed by hand after the agent's write
                continue
            first.setdefault(path, commit)
    return first, flagged


async def _session_commits(
    top: Path, tip: str, session_id: str, stop_seq: int, env: Mapping[str, str]
) -> list[_Commit]:
    """This session's commits from *tip* back, newest first.

    Stops at the first commit that is not this session's checkpoint (the
    history the branch started from), or at the first one at or before
    *stop_seq*, which is included.
    """
    found: list[_Commit] = []
    skip = 0
    while True:
        out = await checked_git(
            top,
            "log",
            "--first-parent",
            "-z",
            "--no-renames",
            "--no-show-signature",
            "--no-ext-diff",
            "--name-status",
            f"--format={_LOG_FORMAT}",
            f"--skip={skip}",
            f"--max-count={_LOG_CHUNK}",
            tip,
            "--",
            env=env,
        )
        records = [r for r in out.split(_LOG_RECORD) if r]
        for record in records:
            commit = _parse_record(record, session_id)
            if commit is None:
                return found
            found.append(commit)
            if commit.log_seq is None or commit.log_seq <= stop_seq:
                return found
        if len(records) < _LOG_CHUNK:
            return found
        skip += _LOG_CHUNK


def _parse_record(record: bytes, session_id: str) -> _Commit | None:
    fields = record.split(b"\x1f", 4)
    if len(fields) != 5:
        return None
    sha, parents, email, body, tail = fields
    if email.decode("utf-8", errors="replace") != AGENT_EMAIL:
        return None
    trailers = parse_trailers(body.decode("utf-8", errors="replace"))
    if trailers is None or trailers.session_id != session_id:
        return None
    items = tail.lstrip(b"\0\n").split(b"\0")
    paths = tuple(
        os.fsdecode(raw)
        for code, raw in zip(items[0::2], items[1::2], strict=False)
        if code and raw
    )
    parent = parents.decode().split()
    return _Commit(
        sha=sha.decode(),
        parent=parent[0] if parent else None,
        kind=trailers.kind,
        log_seq=trailers.log_seq,
        paths=paths,
    )
