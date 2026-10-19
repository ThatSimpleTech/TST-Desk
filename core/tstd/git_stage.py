"""Stage files without filter drivers, and commit them on HEAD (TD-5157).

``hash-object -w --no-filters`` writes bytes as given and
``update-index --index-info`` puts the blob in an index; neither runs a
filter driver (see :mod:`tstd.git_raw`). Line endings are normalised as
``git add`` would (:mod:`tstd.git_eol`), so a checkpoint stores what it
stored before, except that a file git-lfs tracks is stored whole rather
than as a pointer: making the pointer is git-lfs's ``clean``. The
checkpointer, the memory commit and the charter sign stage through here
instead of ``git add``.
"""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .git_eol import checkin_eol, eol_action, eol_config, has_text_crlf, path_attributes
from .git_raw import (
    GitRawError,
    checked_git,
    config_bools,
    filter_pins,
    read_blobs,
    repo_top,
    run_git,
    temp_index,
)

# Hashing a whole folder can take a while on a big tree.
_HASH_TIMEOUT = 300.0
_CHUNK = 256
_LITERAL = "--literal-pathspecs"  # a path is a path, never a glob
_UNFINISHED = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")


@dataclass(frozen=True)
class IndexEntry:
    """One index line: ``mode`` ``"0"`` removes ``path`` (repository-relative, ``/``)."""

    mode: str
    oid: str
    path: str

    def index_info(self) -> bytes:
        return f"{self.mode} {self.oid}\t".encode() + os.fsencode(self.path) + b"\0"


def repo_relative(top: Path, path: Path) -> str:
    """*path* relative to *top*, ``/``-separated; ``"."`` for the top itself.

    The parent is resolved, the last component is not: a symlink is
    staged as a link, as ``git add`` does. ``.git`` is never staged.
    """
    full = Path(os.path.abspath(path))
    if full.resolve() == top:
        return "."
    try:
        rel_parent = full.parent.resolve().relative_to(top)
    except ValueError:
        raise GitRawError(f"'{path}' is outside repository at '{top}'") from None
    rel = (rel_parent / full.name).as_posix()
    if any(part.casefold() == ".git" for part in rel.split("/")):
        raise GitRawError(f"'{rel}' is inside a .git folder; not staging it")
    return rel


async def _ls_stage(
    top: Path, rels: Sequence[str], env: Mapping[str, str]
) -> dict[str, tuple[str, str]]:
    """Index entries at or under *rels*: path to (mode, oid)."""
    entries: dict[str, tuple[str, str]] = {}
    for i in range(0, len(rels), _CHUNK):
        out = await checked_git(
            top, _LITERAL, "ls-files", "-s", "-z", "--", *rels[i : i + _CHUNK], env=env
        )
        for record in out.split(b"\0"):
            meta, tab, name = record.partition(b"\t")
            if not tab:
                continue
            mode, oid, _stage = meta.decode().split(" ")
            entries[os.fsdecode(name)] = (mode, oid)
    return entries


async def _ls_folders(top: Path, rels: Sequence[str], env: Mapping[str, str]) -> list[str]:
    """Tracked and not-ignored files under folders, as ``git add <dir>`` sees them."""
    found: list[str] = []
    for i in range(0, len(rels), _CHUNK):
        out = await checked_git(
            top,
            _LITERAL,
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *rels[i : i + _CHUNK],
            env=env,
        )
        # A trailing "/" is a nested repository; git add would make it a gitlink.
        found += [os.fsdecode(n) for n in out.split(b"\0") if n and not n.endswith(b"/")]
    return list(dict.fromkeys(found))


async def _ignored(top: Path, rels: Sequence[str], env: Mapping[str, str]) -> list[str]:
    if not rels:
        return []
    rc, out, err = await run_git(
        top,
        "check-ignore",
        "-z",
        "--stdin",
        env=env,
        stdin=b"".join(os.fsencode(r) + b"\0" for r in rels),
    )
    if rc not in (0, 1):
        raise GitRawError(f"git check-ignore failed: {err.decode('utf-8', 'replace').strip()}")
    return [os.fsdecode(n) for n in out.split(b"\0") if n]


def _read(top: Path, rels: Sequence[str]) -> dict[str, bytes]:
    return {rel: (top / rel).read_bytes() for rel in rels}


async def _hash(
    top: Path, env: Mapping[str, str], *, write: bool, paths: Sequence[str] = (), data: bytes = b""
) -> list[str]:
    """``hash-object --no-filters`` of *paths* (absolute), or of *data* when none."""
    args = ["hash-object", *(["-w"] if write else []), "--no-filters"]
    if not paths:
        out = await checked_git(top, *args, "--stdin", env=env, stdin=data, timeout=_HASH_TIMEOUT)
        return [out.decode().strip()]
    stdin = b"".join(os.fsencode(p) + b"\n" for p in paths)
    out = await checked_git(
        top, *args, "--stdin-paths", env=env, stdin=stdin, timeout=_HASH_TIMEOUT
    )
    return out.decode().split()


async def blob_ids(
    top: Path,
    rels: Sequence[str],
    tracked: Mapping[str, tuple[str, str]],
    env: Mapping[str, str],
    *,
    write: bool = True,
) -> list[str]:
    """The blob ``git add`` would store for each file, without its filter driver.

    *tracked* maps a path to the (mode, oid) the index holds, for git's
    "safer autocrlf" check. A file git leaves as it is goes to
    ``--stdin-paths`` in one call; a converted one is hashed from memory.
    """
    if not rels:
        return []
    attrs = await path_attributes(top, rels, env, cached=False)
    cfg, _symlinks = await eol_config(top, env)
    texty = [r for r in rels if eol_action(attrs.get(r, {}), cfg) != "binary"]
    contents = await asyncio.to_thread(_read, top, texty)
    auto = [
        r
        for r in texty
        if tracked.get(r, ("",))[0] in ("100644", "100755")
        and b"\r\n" in contents[r]
        and eol_action(attrs.get(r, {}), cfg).startswith("auto")
    ]
    stored = await read_blobs(top, [tracked[r][1] for r in auto], env)
    oids: dict[str, str] = {}
    for rel in texty:
        index_crlf = rel in auto and has_text_crlf(stored[tracked[rel][1]])
        data = contents[rel]
        converted = checkin_eol(data, attrs.get(rel, {}), cfg, index_has_crlf=index_crlf)
        if converted != data:
            oids[rel] = (await _hash(top, env, write=write, data=converted))[0]
    plain = [r for r in rels if r not in oids]
    batch = [r for r in plain if "\n" not in str(top / r)]
    if batch:
        found = await _hash(top, env, write=write, paths=[str(top / r) for r in batch])
        oids.update(zip(batch, found, strict=True))
    for rel in plain:
        if rel not in oids:  # a name --stdin-paths cannot carry
            data = await asyncio.to_thread((top / rel).read_bytes)
            oids[rel] = (await _hash(top, env, write=write, data=data))[0]
    return [oids[rel] for rel in rels]


async def worktree_blob_id(cwd: Path, path: Path, env: Mapping[str, str]) -> str:
    """The blob id staging would give *path* now; nothing is written."""
    top = await repo_top(cwd, env)
    rel = await asyncio.to_thread(repo_relative, top, path)
    tracked = await _ls_stage(top, [rel], env)
    return (await blob_ids(top, [rel], tracked, env, write=False))[0]


async def _hash_link(top: Path, rel: str, env: Mapping[str, str]) -> str:
    target = os.fsencode(await asyncio.to_thread(os.readlink, top / rel))
    out = await checked_git(
        top, "hash-object", "-w", "--no-filters", "--stdin", env=env, stdin=target
    )
    return out.decode().strip()


def _file_mode(st_mode: int, old: tuple[str, str] | None, *, filemode: bool, symlinks: bool) -> str:
    """The index mode ``git add`` would give a regular file."""
    if old is not None and old[0] == "120000" and not symlinks:
        return "120000"  # a checked-out link on a system without symlinks
    if not filemode:
        return old[0] if old is not None and old[0] in ("100644", "100755") else "100644"
    return "100755" if st_mode & stat.S_IXUSR else "100644"


@dataclass
class _Plan:
    """What staging does to each path: hash a file (with its mode), hash a link, remove."""

    files: dict[str, int]
    links: list[str]
    removes: list[str]
    folders: list[str]


def _plan(
    top: Path, rels: Sequence[str], tracked: Mapping[str, tuple[str, str]], *, explicit: bool
) -> _Plan:
    """lstat each path. A missing one removes what is tracked at or under it.

    *explicit* paths came from the caller, so a missing untracked one is
    an error, as for ``git add``; listed ones (a folder's contents) are not.
    """
    plan = _Plan({}, [], [], [])
    for rel in rels:
        if not explicit and tracked.get(rel, ("",))[0] == "160000":
            continue  # a submodule: git add leaves it alone
        try:
            st = os.lstat(top / rel)
        except (FileNotFoundError, NotADirectoryError):
            under = [t for t in tracked if t == rel or t.startswith(rel + "/")]
            if explicit and not under:
                raise GitRawError(f"pathspec '{rel}' did not match any files") from None
            plan.removes += under
            continue
        if stat.S_ISDIR(st.st_mode):
            if explicit:
                plan.folders.append(rel)
        elif stat.S_ISLNK(st.st_mode):
            plan.links.append(rel)
        elif stat.S_ISREG(st.st_mode):
            plan.files[rel] = st.st_mode
        elif explicit:
            raise GitRawError(f"'{rel}' is not a file, link or folder")
    return plan


async def stage_paths(
    top: Path, paths: Sequence[Path], *, env: Mapping[str, str]
) -> list[IndexEntry]:
    """Put *paths* into the index ``GIT_INDEX_FILE`` in *env* names, as raw bytes.

    Mirrors ``git add -- <paths>`` without filters: a file or link is
    hashed as it is on disk, a missing tracked path is removed, a folder
    stages what ``git add`` would under it, and an untracked ignored path
    is refused the way ``git add`` refuses it.
    """
    rels = list(
        dict.fromkeys(await asyncio.to_thread(lambda: [repo_relative(top, p) for p in paths]))
    )
    tracked = await _ls_stage(top, rels, env)
    bools = await config_bools(top, env, "core.filemode", "core.symlinks")
    filemode = bools.get("core.filemode", os.name != "nt")
    symlinks = bools.get("core.symlinks", os.name != "nt")

    plan = await asyncio.to_thread(_plan, top, rels, tracked, explicit=True)
    explicit = [*plan.files, *plan.links]
    if plan.folders:
        tracked.update(await _ls_stage(top, plan.folders, env))
        listed = await _ls_folders(top, plan.folders, env)
        inner = await asyncio.to_thread(_plan, top, listed, tracked, explicit=False)
        plan.files.update(inner.files)
        plan.links += inner.links
        plan.removes += inner.removes

    ignored = await _ignored(top, [r for r in explicit if r not in tracked], env)
    if ignored:
        raise GitRawError(
            "The following paths are ignored by one of your .gitignore files: " + ", ".join(ignored)
        )

    entries: list[IndexEntry] = []
    files = list(plan.files)
    for rel, oid in zip(files, await blob_ids(top, files, tracked, env), strict=True):
        mode = _file_mode(plan.files[rel], tracked.get(rel), filemode=filemode, symlinks=symlinks)
        entries.append(IndexEntry(mode, oid, rel))
    for rel in dict.fromkeys(plan.links):
        entries.append(IndexEntry("120000", await _hash_link(top, rel, env), rel))
    for rel in dict.fromkeys(plan.removes):
        entries.append(IndexEntry("0", "0" * len(tracked[rel][1]), rel))
    if entries:
        await checked_git(
            top,
            "update-index",
            "-z",
            "--index-info",
            env=env,
            stdin=b"".join(e.index_info() for e in entries),
        )
    return entries


async def commit_on_head(
    cwd: Path, paths: Sequence[Path], message: str, *, env: Mapping[str, str]
) -> str | None:
    """Commit *paths* as they are on disk on top of HEAD; ``None`` when nothing changed.

    What ``git commit -m <message> -- <paths>`` does, without filters or
    hooks: only these paths change, the user's other staged work is left
    staged, HEAD (or the branch it names) moves with a reflog line, and
    the user's index gets the committed entries. ``commit-tree`` signs
    only when asked, so ``-S`` is passed when ``commit.gpgSign`` is set,
    as ``git commit`` would.
    """
    top = await repo_top(cwd, env)
    out = await checked_git(top, "rev-parse", "--git-dir", env=env)
    git_dir = top / os.fsdecode(out.strip())  # an absolute git dir replaces top
    for name in _UNFINISHED:
        if await asyncio.to_thread(os.path.lexists, git_dir / name):
            raise GitRawError(f"cannot do a partial commit while {name} exists")
    rc, out, _err = await run_git(top, "rev-parse", "--verify", "-q", "HEAD^{commit}", env=env)
    parent = out.decode().strip() if rc == 0 and out.strip() else None

    with temp_index("tstd-commit-index-") as index:
        index_env = {**env, "GIT_INDEX_FILE": index}
        if parent is not None:
            await checked_git(top, "read-tree", parent, env=index_env)
        entries = await stage_paths(top, paths, env=index_env)
        tree = (await checked_git(top, "write-tree", env=index_env)).decode().strip()
    if parent is not None:
        base = await checked_git(top, "rev-parse", f"{parent}^{{tree}}", env=env)
        if base.decode().strip() == tree:
            return None

    args = ["commit-tree", tree]
    if parent is not None:
        args += ["-p", parent]
    if (await config_bools(top, env, "commit.gpgsign")).get("commit.gpgsign", False):
        args.append("-S")
    commit = (await checked_git(top, *args, "-m", message, env=env)).decode().strip()

    # The user's index first, as `git add` would: if HEAD moves under us,
    # the paths are only staged. Writing an index hashes racily clean
    # entries through their filter, hence the pins.
    pins = await filter_pins(top, env)
    await checked_git(
        top,
        *pins,
        "update-index",
        "-z",
        "--index-info",
        env=env,
        stdin=b"".join(e.index_info() for e in entries),
    )
    subject = message.splitlines()[0] if message else ""
    reflog = f"commit: {subject}" if parent is not None else f"commit (initial): {subject}"
    await checked_git(
        top, "update-ref", "-m", reflog, "HEAD", commit, parent or "0" * len(commit), env=env
    )
    return commit
