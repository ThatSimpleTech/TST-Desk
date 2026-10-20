"""Stage and commit files without git's filter drivers (TD-5157).

``git add``, ``git commit``, ``git status`` and ``checkout-index`` run the
filter driver an attribute names: ``clean`` on the way in, ``smudge`` on
the way out, or one long-running ``process``. The command comes from git
config, which is Class C to write (TD-5108), but the attribute that routes
a file through it can come from an in-tree ``.gitattributes``, an ordinary
Class A write. So one write could make the daemon's own git run a filter
the user configured (``python -m nbstripout`` imports from the repo root
first; ``git-lfs`` reads ``.lfsconfig``) outside the wall.

So the daemon stages bytes itself (:mod:`tstd.git_stage`) and writes
them back itself (:mod:`tstd.git_restore`). This module is what both
share: the git runner, a throwaway index, and the pins below.

A few calls cannot avoid the working tree: ``git status`` for the
dirty-tree notice, the validator's ``git diff``, and writing the user's
own index after a memory or charter commit (git hashes a "racily clean"
entry through its filter whenever it writes an index). Those run with
:func:`filter_pins`, which sets every configured filter driver to nothing
with ``-c`` for that one call. The pins name only the drivers this repo's
config can see, so ``status`` and ``diff`` also pass
``--ignore-submodules=dirty``: git would otherwise run inside each
submodule, under the submodule's own config.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path

from .git_safe import git_command
from .proc_lifecycle import finish_subprocess

_GIT_TIMEOUT = 30.0


class GitRawError(RuntimeError):
    """A git step failed; the message carries git's own words where it has them."""


async def run_git(
    cwd: Path,
    *args: str,
    env: Mapping[str, str],
    stdin: bytes | int | None = None,
    timeout: float = _GIT_TIMEOUT,  # noqa: ASYNC109 - passed to finish_subprocess
) -> tuple[int, bytes, bytes]:
    """``git -C <cwd> <args>`` through :func:`git_command`; bytes in and out.

    *stdin* is bytes to write, or an open file descriptor git reads itself
    (a file the caller opened and checked, TD-5158).
    """
    if isinstance(stdin, int):
        spawn_stdin: int = stdin
    else:
        spawn_stdin = asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL
    proc = await asyncio.create_subprocess_exec(
        *git_command("-C", str(cwd), *args),
        stdin=spawn_stdin,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env),
    )
    written = stdin if isinstance(stdin, bytes) else None
    out, err = await finish_subprocess(proc, timeout=timeout, stdin=written)
    return (proc.returncode if proc.returncode is not None else -1), out, err


async def checked_git(
    cwd: Path,
    *args: str,
    env: Mapping[str, str],
    stdin: bytes | int | None = None,
    timeout: float = _GIT_TIMEOUT,  # noqa: ASYNC109 - passed to finish_subprocess
) -> bytes:
    """:func:`run_git`, raising :class:`GitRawError` on a non-zero exit."""
    rc, out, err = await run_git(cwd, *args, env=env, stdin=stdin, timeout=timeout)
    if rc != 0:
        verb = next((a for a in args if not a.startswith("-") and "=" not in a), "git")
        detail = err.decode("utf-8", errors="replace").strip()
        raise GitRawError(f"git {verb} failed: {detail}" if detail else f"git {verb} failed")
    return out


@contextlib.contextmanager
def temp_index(prefix: str) -> Iterator[str]:
    """A path for a throwaway index file, removed afterwards."""
    fd, path = tempfile.mkstemp(prefix=prefix)
    os.close(fd)
    # Git must create the index itself: a zero-byte file reads as corrupt.
    os.unlink(path)
    try:
        yield path
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


async def repo_top(cwd: Path, env: Mapping[str, str]) -> Path:
    """The work tree's top folder, resolved."""
    out = await checked_git(cwd, "rev-parse", "--show-toplevel", env=env)
    return await asyncio.to_thread(Path(os.fsdecode(out.rstrip(b"\r\n"))).resolve)


async def filter_pins(cwd: Path, env: Mapping[str, str]) -> list[str]:
    """``-c`` arguments that switch off every filter driver git config names.

    A driver is ``filter.<name>.clean``, ``.smudge`` or ``.process``; all
    three are set to nothing and ``required`` to false, so an attribute
    naming it does nothing. A name ``-c`` cannot carry (one with ``=``)
    raises rather than leaving that driver on.
    """
    rc, out, err = await run_git(
        cwd, "config", "--null", "--name-only", "--get-regexp", r"^filter\.", env=env
    )
    if rc == 1:  # no filter keys at all
        return []
    if rc != 0:
        raise GitRawError(f"git config failed: {err.decode('utf-8', errors='replace').strip()}")
    names: set[str] = set()
    for raw in out.split(b"\0"):
        key = os.fsdecode(raw.strip(b"\n"))
        name, dot, _var = key.removeprefix("filter.").rpartition(".")
        if dot and name:
            names.add(name)
    pins: list[str] = []
    for name in sorted(names):
        if "=" in name or "\n" in name:
            raise GitRawError(f"cannot switch off filter driver {name!r}")
        for var, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
            pins += ["-c", f"filter.{name}.{var}={value}"]
    return pins


async def config_bools(cwd: Path, env: Mapping[str, str], *keys: str) -> dict[str, bool]:
    """Boolean config values for *keys* (lower-case ``section.name``) that are set."""
    pattern = "^(" + "|".join(k.replace(".", r"\.") for k in keys) + ")$"
    rc, out, _err = await run_git(
        cwd, "config", "-z", "--type=bool", "--get-regexp", pattern, env=env
    )
    found: dict[str, bool] = {}
    if rc != 0:
        return found
    for item in out.split(b"\0"):
        key, _nl, value = item.decode("utf-8", errors="replace").partition("\n")
        if key:
            found[key] = value.strip() == "true"
    return found


async def read_blobs(cwd: Path, oids: list[str], env: Mapping[str, str]) -> dict[str, bytes]:
    """Raw blob bytes by id, through ``cat-file --batch`` (which never filters)."""
    unique = list(dict.fromkeys(oids))
    if not unique:
        return {}
    out = await checked_git(
        cwd, "cat-file", "--batch", env=env, stdin="".join(f"{o}\n" for o in unique).encode()
    )
    blobs: dict[str, bytes] = {}
    pos = 0
    for oid in unique:
        end = out.index(b"\n", pos)
        header = out[pos:end].split(b" ")
        if len(header) != 3 or header[1] != b"blob":
            raise GitRawError(f"git cat-file: {oid} is not a blob")
        size = int(header[2])
        blobs[oid] = out[end + 1 : end + 1 + size]
        pos = end + 1 + size + 1
    return blobs
