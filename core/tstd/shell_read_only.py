"""The static read-only command list for shell (TD-5103).

TD-4805 put every shell command at Class B: the static table cannot see
inside a command string, and a model's reading of one must never be the
gate on running it. This list is the one exception Adam approved on
2026-10-14. It is fixed in code and parsed, not judged: a command made only
of the entries below, read by :mod:`tstd.shell_read_only_args`, is Class A
by the ``shell-read-only`` rule and runs without asking. Nothing a model
says can add to it, and the user's config can only shrink it or turn it off
(``shell.read_only`` in ``config.yaml``).

Anything else drops back to the ``shell-floor`` rule and asks as before:
a redirect, a command off the list, a write, exec or symlink-following
flag, a path that resolves outside the workspace, a program that resolves
inside it or through a relative ``PATH`` entry, or a line the TD-5101
scanner cannot split with confidence. On Windows, where commands run
through ``cmd /c``, nothing matches.

The user's ``ask`` and ``never`` rules for the built-in reads (TD-5141)
guard files from the list too: while any is in force, a command that
names a file one of them matches, or that can print a file no word names
(a recursive grep, ``rg``, ``git diff``, ``show`` or ``log``, ``git
status -v``), drops back as well.

git reads its own config and attributes, and both can name a command:
``core.fsmonitor``, a ``post-index-change`` hook in a ``core.hooksPath``
folder that sits in the tree, or a filter or textconv driver that an
in-tree ``.gitattributes`` routes a file through. So a git entry matches
only when the workspace root holds its own ``.git`` (a parent repository
would show files outside the wall) and git is 2.40 or newer, and a
matching command runs under :func:`run_env`: :data:`GIT_SAFE_CONFIG`'s
pins, no attributes but ``.git/info/attributes`` (in-tree ones read from
the empty tree, ``core.attributesFile`` pointed at nothing), and no
optional index write.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .git_safe import GIT_SAFE_CONFIG
from .policy_shell import host_dialects, split_command
from .proc_lifecycle import finish_subprocess
from .shell_read_only_args import SPECS, find_args, git_args, option_args, segments

__all__ = [
    "READ_ONLY_ENTRIES",
    "git_ready",
    "match",
    "probe_git",
    "run_env",
]

# The packaged list, and the only entries config may name.
READ_ONLY_ENTRIES: tuple[str, ...] = (
    "ls",
    "pwd",
    "cat",
    "head",
    "tail",
    "wc",
    "file",
    "stat",
    "git status",
    "git log",
    "git diff",
    "git show",
    "git branch",
    "rg",
    "grep",
    "find",
)

# GIT_ATTR_SOURCE came in git 2.40. An older git ignores it, and an
# in-tree attribute would route files through a filter again.
_GIT_MIN = (2, 40)
_GIT_VERSION = re.compile(rb"git version (\d+)\.(\d+)")
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_PROBE_TIMEOUT = 10.0
# Resolved git path → whether it is new enough. Filled by probe_git.
_git_probe: dict[str, bool] = {}


@dataclass(frozen=True)
class _Call:
    entry: str
    checks: tuple[str, ...]
    walks: bool


def _calls(command: str, entries: frozenset[str]) -> list[_Call] | None:
    """Each command of the line as a list entry, or None."""
    if not entries or "cmd" in host_dialects():
        return None
    segs = segments(command)
    if segs is None or not split_command(command, ("posix",)).complete:
        return None
    calls: list[_Call] = []
    for argv in segs:
        program, args = argv[0], argv[1:]
        if program == "git":
            parsed = git_args(args)
            if parsed is None:
                return None
            entry, read = f"git {parsed[0]}", parsed[1]
        elif program == "find":
            entry, found = program, find_args(args)
            if found is None:
                return None
            read = found
        elif program in SPECS:
            entry, opts = program, option_args(SPECS[program], args)
            if opts is None:
                return None
            read = opts
        else:
            return None
        if entry not in entries:
            return None
        calls.append(_Call(entry, read.checks, read.walks))
    return calls


def _real(path: str) -> Path:
    return Path(os.path.abspath(os.path.realpath(path)))


def _inside(word: str, root: Path, real_root: Path) -> bool:
    return _real(os.path.join(root, word)).is_relative_to(real_root)


def _guarded(word: str, guards: Sequence[str], root: Path, real_root: Path) -> bool:
    """Whether a guard glob matches the file *word* names.

    Matched as written, normalized, and workspace-relative with symlinks
    resolved, the form an ``fs_read`` rule sees, and ignoring case, as
    ``ask`` and ``never`` rules match (TD-5141).
    """
    forms = {word, os.path.normpath(word)}
    real = _real(os.path.join(root, word))
    if real.is_relative_to(real_root):
        forms.add(real.relative_to(real_root).as_posix())
    return any(
        fnmatch.fnmatchcase(form, glob) or fnmatch.fnmatchcase(form.casefold(), glob.casefold())
        for form in forms
        for glob in guards
    )


def _program_outside(name: str, real_root: Path) -> bool:
    """Whether the shell would run *name* from outside the workspace.

    An empty or relative ``PATH`` entry is looked up from the shell's
    working directory, which is the workspace, so it refuses every name.
    """
    search = os.environ.get("PATH", os.defpath)
    if any(not entry or not os.path.isabs(entry) for entry in search.split(os.pathsep)):
        return False
    found = shutil.which(name, path=search)
    return found is not None and not _real(found).is_relative_to(real_root)


def _env_inside(name: str, root: Path, real_root: Path) -> bool:
    """Whether the file the variable *name* names lies inside the workspace."""
    value = os.environ.get(name)
    return bool(value) and _inside(value or "", root, real_root)


def _git_ok(root: Path, real_root: Path) -> bool:
    """git 2.40+, the workspace's own repository, and config the agent cannot write.

    ``GIT_DIR`` or ``GIT_WORK_TREE`` would point git at another repository,
    and a global or system config file inside the workspace is an ordinary
    file there that can name ``diff.external``.
    """
    if not git_ready() or not os.path.lexists(root / ".git"):
        return False
    if os.environ.get("GIT_DIR") or os.environ.get("GIT_WORK_TREE"):
        return False
    return not any(
        _env_inside(name, root, real_root) for name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")
    )


def match(
    command: str, entries: frozenset[str], root: Path | None, guards: Sequence[str] = ()
) -> tuple[str, ...]:
    """The list entries that cover every command of *command*, in order.

    Empty when the line does not qualify: any command off the list or
    using a refused flag, a word that resolves outside *root* (symlinks
    resolved), a program found inside *root*, for git anything
    :func:`_git_ok` refuses, or for rg a config file inside *root* (one
    line of it can add ``--pre``).

    *guards* are the ``args`` globs of the user's ``ask`` and ``never``
    rules for the built-in reads (:func:`tstd.policy.read_guards`). While
    there is one, a word one matches, or a command that can print a file
    no word names, empties the match too.
    """
    calls = _calls(command, entries)
    if calls is None or root is None:
        return ()
    real_root = _real(str(root))
    for call in calls:
        program = call.entry.split(" ", 1)[0]
        if guards and call.walks:
            return ()
        if not _program_outside(program, real_root):
            return ()
        if program == "git" and not _git_ok(root, real_root):
            return ()
        if program == "rg" and _env_inside("RIPGREP_CONFIG_PATH", root, real_root):
            return ()
        if not all(_inside(word, root, real_root) for word in call.checks):
            return ()
        if guards and any(_guarded(word, guards, root, real_root) for word in call.checks):
            return ()
    return tuple(dict.fromkeys(call.entry for call in calls))


def run_env(command: str, entries: frozenset[str], env: Mapping[str, str]) -> dict[str, str]:
    """Variables to add to *env* when *command* runs: git's pins, or nothing.

    Applied to any command whose words the list reads as a git entry,
    whoever approved it, so the pins never hang on a path check made
    before the command ran. ``GIT_CONFIG_COUNT`` adds to the caller's own
    ``-c`` style pairs rather than replacing them.
    """
    calls = _calls(command, entries)
    if calls is None or not any(c.entry.startswith("git ") for c in calls):
        return {}
    raw = env.get("GIT_CONFIG_COUNT", "0")
    base = int(raw) if raw.isdigit() else 0
    pairs = [GIT_SAFE_CONFIG[i + 1].split("=", 1) for i in range(0, len(GIT_SAFE_CONFIG), 2)]
    # The user's attributes file may sit in the workspace (a dotfiles repo,
    # or a workspace that is the home folder) and route files as well.
    pairs.append(["core.attributesFile", os.devnull])
    out = {
        "GIT_CONFIG_COUNT": str(base + len(pairs)),
        # In-tree .gitattributes are read from the empty tree: no filter,
        # textconv or diff driver routing. Only .git/info/attributes still
        # applies, and it is Class C to write (TD-5108).
        "GIT_ATTR_SOURCE": _EMPTY_TREE,
        # `git status` refreshes the index when it can; this keeps it a read.
        "GIT_OPTIONAL_LOCKS": "0",
        # A partial clone would fetch a missing blob from the network.
        "GIT_NO_LAZY_FETCH": "1",
    }
    for offset, (key, value) in enumerate(pairs):
        out[f"GIT_CONFIG_KEY_{base + offset}"] = key
        out[f"GIT_CONFIG_VALUE_{base + offset}"] = value
    return out


def git_ready() -> bool:
    """Whether :func:`probe_git` found the ``PATH`` git to be 2.40 or newer."""
    found = shutil.which("git")
    return found is not None and _git_probe.get(found, False)


async def probe_git() -> None:
    """Read ``git --version`` once per git binary, off the event loop's back.

    Until it has run, git entries never match: the list fails toward asking.
    """
    found = shutil.which("git")
    if found is None or found in _git_probe or "cmd" in host_dialects():
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            found,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _err = await finish_subprocess(proc, timeout=_PROBE_TIMEOUT)
    except (OSError, TimeoutError):
        _git_probe[found] = False
        return
    version = _GIT_VERSION.search(out)
    _git_probe[found] = version is not None and ((int(version[1]), int(version[2])) >= _GIT_MIN)
