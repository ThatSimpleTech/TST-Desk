"""The argv every git call the daemon makes starts with (TD-5108).

A repository's own config can name a command for git to run.
``core.fsmonitor`` runs on ``status``, ``add`` and ``ls-files``, so one
write to ``.git/config`` used to become a command the checkpointer ran
after the next file write, with no approval and outside the wall. A folder
holding ``HEAD``, ``config``, ``objects/`` and ``refs/`` is also a bare repo
that git finds on its own and reads config from.

Command-line ``-c`` beats every config file and include, so both are
pinned here: fsmonitor off, and bare repos only when named explicitly.
``fs_grep`` adopted the same pair first (TD-5141).
"""

from __future__ import annotations

GIT_SAFE_CONFIG: tuple[str, ...] = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "safe.bareRepository=explicit",
)


def git_command(*args: str) -> list[str]:
    """``git`` plus :data:`GIT_SAFE_CONFIG`, then *args*."""
    return ["git", *GIT_SAFE_CONFIG, *args]
