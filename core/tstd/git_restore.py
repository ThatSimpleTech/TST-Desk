"""Write blobs from a commit into the working tree without filter drivers (TD-5157).

``checkout-index`` runs the ``smudge`` filter (or ``process``) an
attribute names, and the attribute can come from an in-tree
``.gitattributes`` (see :mod:`tstd.git_raw`). The revert writes the bytes
itself instead, after the parts of a checkout that run no command:

- End-of-line conversion is git's own rule set (:mod:`tstd.git_eol`),
  read from the restored commit's ``.gitattributes``: a blob stored as
  LF gets CRLF back where a checkout would.
- A git-lfs pointer for a ``filter=lfs`` path is replaced by the object
  from the local store (``<git-common-dir>/lfs/objects``) when it is
  there and its size and SHA-256 match. Nothing is fetched. Otherwise the
  pointer text is written and the path is reported; ``git lfs checkout``
  fills it in.
- ``ident`` and ``working-tree-encoding`` are not applied.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .git_eol import checkout_eol, eol_config, path_attributes
from .git_raw import GitRawError, checked_git, read_blobs, repo_top, temp_index

_CHUNK = 256
_LITERAL = "--literal-pathspecs"
_LFS_VERSIONS = (b"https://git-lfs.github.com/spec/v1", b"https://hawser.github.com/spec/v1")
_LFS_OID = re.compile(rb"^sha256:([0-9a-f]{64})$")


@dataclass(frozen=True)
class RestoreResult:
    """Paths written, and those left as an LFS pointer (object not stored locally)."""

    restored: tuple[str, ...]
    pointers: tuple[str, ...]


def lfs_pointer(data: bytes) -> tuple[str, int] | None:
    """``(oid, size)`` when *data* is a git-lfs pointer file."""
    if len(data) > 1024 or not data.startswith(b"version "):
        return None
    fields: dict[bytes, bytes] = {}
    for line in data.split(b"\n"):
        if line:
            key, _sp, value = line.partition(b" ")
            fields[key] = value
    match = _LFS_OID.match(fields.get(b"oid", b""))
    size = fields.get(b"size", b"")
    if fields.get(b"version") not in _LFS_VERSIONS or match is None or not size.isdigit():
        return None
    return match.group(1).decode(), int(size)


async def _entries(
    top: Path, rels: Sequence[str], env: Mapping[str, str]
) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    wanted = set(rels)
    for i in range(0, len(rels), _CHUNK):
        out = await checked_git(
            top, _LITERAL, "ls-files", "-s", "-z", "--", *rels[i : i + _CHUNK], env=env
        )
        for record in out.split(b"\0"):
            meta, tab, raw = record.partition(b"\t")
            name = os.fsdecode(raw)
            if tab and name in wanted:
                mode, oid, _stage = meta.decode().split(" ")
                found[name] = (mode, oid)
    return found


async def _lfs_store(top: Path, env: Mapping[str, str]) -> Path:
    out = await checked_git(top, "rev-parse", "--git-common-dir", env=env)
    common = Path(os.fsdecode(out.strip()))
    return (common if common.is_absolute() else top / common) / "lfs" / "objects"


# On Windows ``\`` and ``:`` would be read again as a separator or a drive or
# stream; git for Windows refuses such a name too ("invalid path").
_NT = os.name == "nt"


def _parent(top: Path, rel: str) -> Path:
    """The folder *rel* goes in, made as needed; never through a symlink."""
    parts = PurePosixPath(rel).parts
    if not parts or rel.startswith("/") or any(p in (".", "..") for p in parts):
        raise GitRawError(f"refusing to restore '{rel}'")
    if _NT and ("\\" in rel or ":" in rel):
        raise GitRawError(f"invalid path '{rel}'")
    if any(p.casefold() == ".git" for p in parts):
        raise GitRawError(f"refusing to restore '{rel}' inside a .git folder")
    folder = top
    for part in parts[:-1]:
        folder = folder / part
        try:
            st = os.lstat(folder)
        except FileNotFoundError:
            os.mkdir(folder)
            continue
        if not stat.S_ISDIR(st.st_mode):
            raise GitRawError(f"cannot restore '{rel}': '{folder}' is not a folder")
    return folder


def _replace(target: Path, fill: Callable[[Path], None]) -> None:
    """Fill a fresh sibling, then rename it over *target* (a link is replaced, not followed)."""
    if target.is_dir() and not target.is_symlink():
        raise GitRawError(f"cannot restore '{target}': a folder is in the way")
    tmp = target.with_name(f".{target.name}.tstd-{secrets.token_hex(4)}")
    try:
        fill(tmp)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_bytes(target: Path, data: bytes, executable: bool) -> None:
    def fill(tmp: Path) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(tmp, flags, 0o777 if executable else 0o666)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

    _replace(target, fill)


def _copy_verified(target: Path, source: Path, oid: str, size: int, executable: bool) -> bool:
    """Copy an LFS object if it is a plain file whose size and SHA-256 match the pointer."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        src_fd = os.open(source, flags)
    except OSError:
        return False
    with os.fdopen(src_fd, "rb") as src:
        st = os.fstat(src.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size != size:
            return False
        digest = hashlib.sha256()

        def fill(tmp: Path) -> None:
            out_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            out_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            fd = os.open(tmp, out_flags, 0o777 if executable else 0o666)
            left = size
            with os.fdopen(fd, "wb") as out:
                while left > 0 and (chunk := src.read(min(left, 1 << 20))):
                    digest.update(chunk)
                    out.write(chunk)
                    left -= len(chunk)
            if left or digest.hexdigest() != oid:
                raise _Mismatch

        try:
            _replace(target, fill)
        except _Mismatch:
            return False
    return True


class _Mismatch(Exception):
    """The local LFS object is not the one the pointer names."""


def _write_link(target: Path, text: bytes, symlinks: bool) -> None:
    if not symlinks:
        _write_bytes(target, text, executable=False)  # as git does with core.symlinks=false
        return
    _replace(target, lambda tmp: os.symlink(os.fsdecode(text), tmp))


async def restore_blobs(
    cwd: Path, treeish: str, paths: Sequence[str], *, env: Mapping[str, str]
) -> RestoreResult:
    """Write each of *paths* (repository-relative) as *treeish* has it."""
    if not paths:
        return RestoreResult((), ())
    top = await repo_top(cwd, env)
    rels = list(dict.fromkeys(paths))
    with temp_index("tstd-restore-index-") as index:
        index_env = {**env, "GIT_INDEX_FILE": index}
        await checked_git(top, "read-tree", treeish, env=index_env)
        entries = await _entries(top, rels, index_env)
        attrs = await path_attributes(top, list(entries), index_env, cached=True)
    cfg, symlinks = await eol_config(top, env)
    blobs = await read_blobs(top, [oid for mode, oid in entries.values() if mode != "160000"], env)
    lfs = any(a.get("filter") == "lfs" for a in attrs.values())
    store = await _lfs_store(top, env) if lfs else None

    def write_all() -> RestoreResult:
        restored: list[str] = []
        pointers: list[str] = []
        for rel, (mode, oid) in entries.items():
            if mode == "160000":
                continue  # a submodule commit; checkout-index skips it too
            target = _parent(top, rel) / PurePosixPath(rel).name
            data = blobs[oid]
            if mode == "120000":
                _write_link(target, data, symlinks)
                restored.append(rel)
                continue
            executable = mode == "100755"
            pointer = lfs_pointer(data) if attrs.get(rel, {}).get("filter") == "lfs" else None
            if pointer is not None and store is not None:
                lfs_oid, size = pointer
                source = store / lfs_oid[:2] / lfs_oid[2:4] / lfs_oid
                if not _copy_verified(target, source, lfs_oid, size, executable):
                    _write_bytes(target, data, executable)
                    pointers.append(rel)
            else:
                _write_bytes(target, checkout_eol(data, attrs.get(rel, {}), cfg), executable)
            restored.append(rel)
        return RestoreResult(tuple(restored), tuple(pointers))

    return await asyncio.to_thread(write_all)
