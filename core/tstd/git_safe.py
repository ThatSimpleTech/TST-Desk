"""The argv every git call the daemon makes starts with (TD-5108).

A repository's own config can name a command for git to run.
``core.fsmonitor`` runs on ``status``, ``add`` and ``ls-files``, so one
write to ``.git/config`` used to become a command the checkpointer ran
after the next file write, with no approval and outside the wall. A folder
holding ``HEAD``, ``config``, ``objects/`` and ``refs/`` is also a bare repo
that git finds on its own and reads config from.

Hooks are the same kind of hole. The memory and charter commits run
``git commit``, ``update-ref`` and ``add`` fire ``reference-transaction``
and ``post-index-change``, and the GitHub connector's push fires
``pre-push``. A hook in a ``core.hooksPath`` folder the user set, or a
workspace script an existing hook calls, is an ordinary Class A edit, so
the next memory write made the daemon run it. ``core.hooksPath`` here
names a path no file can live under, so the daemon's own git runs no
hooks at all. The user's own ``git commit`` still runs them.

Command-line ``-c`` beats every config file and include, so all three are
pinned here: fsmonitor off, hooks off, and bare repos only when named
explicitly. ``fs_grep`` adopted the first and last first (TD-5141).

Filter drivers cannot be pinned here: their names are the user's. The
daemon avoids them instead, staging and restoring bytes itself and
switching the configured ones off where it must read the working tree
(``git_raw``, ``git_stage``, ``git_restore``; TD-5157). Not pinned:
``gpg.program`` with ``commit.gpgSign``, and ``core.askPass``. They run
only when a config file names them, and those files are Class C to write
(DECISIONS.md, TD-5108, residual risk).
"""

from __future__ import annotations

import os

GIT_SAFE_CONFIG: tuple[str, ...] = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    # /dev/null (nul on Windows) is not a folder, so <it>/pre-commit
    # never exists and no hook is found.
    f"core.hooksPath={os.devnull}",
    "-c",
    "safe.bareRepository=explicit",
)


def git_command(*args: str) -> list[str]:
    """``git`` plus :data:`GIT_SAFE_CONFIG`, then *args*."""
    return ["git", *GIT_SAFE_CONFIG, *args]
