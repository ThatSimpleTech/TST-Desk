"""Copies of the files the agent sends to the chat (TD-5143).

``send_file`` copies each workspace file into the session's own data dir,
``sessions/<sid>/artifacts/<aid>/<name>``, and indexes it as a
``sent_file`` artifact. The copy is what the user downloads, so a later
rebuild of the source never changes what was sent. A call is all or
nothing: any refusal or failure leaves no copy, no index row and no event.

Bytes never reach a log, the event log or the audit DB (AGENTS.md). Only
``read_chunk`` reads a copy back, for the connection-scoped
``artifact_chunk`` reply.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .artifacts import ArtifactError, ArtifactRecord, ArtifactStore
from .attachments import format_bytes
from .autonomy.classifier import Boundary, canonical_path
from .logging import redact_secrets
from .protocol import CHUNK_MAX
from .tools.boundary import PathGuard, RefusalError

if TYPE_CHECKING:
    from .config import SessionConfig

# Free space that must remain after the copies, so a send never fills the disk.
FREE_MARGIN = 256 * 1024 * 1024
_BLOCK = 1024 * 1024
_PART_MAX_AGE_S = 3600.0
_PART_NAME = re.compile(r"\.[0-9a-f]{32}\.part")
# Per-session locks, process-wide: parallel sends in one batch serialize per
# session even if two runtimes ever build a ``SentFiles`` for the same id.
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()

# O_NONBLOCK keeps a FIFO from blocking the open; it changes nothing for a
# regular file. Missing flags (Windows) are 0.
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)
_CHUNK_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)
_PART_FLAGS = (
    os.O_CREAT
    | os.O_EXCL
    | os.O_WRONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)

# ── File names (§1.4; identical in Python, Rust and Kotlin) ───────────

_NAME_MAX_BYTES = 120
_EXT_MAX_CHARS = 16
_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_SEPARATORS = re.compile(r"[/\\]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_RESERVED = re.compile(r'[:*?"<>|]')
# Python only: an undecodable name (surrogateescape) holds lone surrogates,
# which UTF-8 cannot encode. They become U+FFFD, as a lossy decode does.
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _device_prefixed(name: str) -> str:
    head = name.split(".", 1)[0].rstrip(" ")
    return f"_{name}" if head.upper() in _DEVICE_NAMES else name


def _cut_utf8(text: str, budget: int) -> str:
    """The longest prefix of *text* that fits *budget* UTF-8 bytes."""
    return text.encode("utf-8")[:budget].decode("utf-8", errors="ignore")


def _truncated(name: str) -> str:
    if len(name.encode("utf-8")) <= _NAME_MAX_BYTES:
        return name
    dot = name.rfind(".")
    if dot > 0 and len(name) - dot <= _EXT_MAX_CHARS:
        ext = name[dot:]
        return _cut_utf8(name[:dot], _NAME_MAX_BYTES - len(ext.encode("utf-8"))) + ext
    return _cut_utf8(name, _NAME_MAX_BYTES)


def safe_file_name(raw: str) -> str:
    """A file name that is safe on every OS the file can land on (§1.4)."""
    name = _SEPARATORS.split(raw)[-1]  # 1. basename
    name = _CONTROL.sub("", _SURROGATE.sub("�", name))  # 2. controls
    name = _RESERVED.sub("_", name)  # 3. reserved chars
    name = name.rstrip(" .")  # 4. trailing spaces and dots
    name = _device_prefixed(name)  # 5. device names
    name = _truncated(name)  # 6. 120 UTF-8 bytes, extension kept
    name = name.rstrip(" .")  # 7. again
    name = _truncated(_device_prefixed(name))  # 8. again; a new "_" can need one more cut
    return name or "file"  # 9. never empty


# ── MIME (§1.5; fixed, never ``mimetypes``, which reads host files) ───

_OCTET = "application/octet-stream"
_OOXML = "application/vnd.openxmlformats-officedocument"
_MIME: dict[str, str] = {
    ".apk": "application/vnd.android.package-archive",
    ".aab": _OCTET,
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
    ".7z": "application/x-7z-compressed",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
    ".docx": f"{_OOXML}.wordprocessingml.document",
    ".xlsx": f"{_OOXML}.spreadsheetml.sheet",
    ".pptx": f"{_OOXML}.presentationml.presentation",
    ".dmg": "application/x-apple-diskimage",
}


def mime_for(name: str) -> str:
    """The MIME type for *name*, by its last extension, case-insensitive."""
    dot = name.rfind(".")
    if dot <= 0:
        return _OCTET
    return _MIME.get(name[dot:].lower(), _OCTET)


# ── Result text (§1.2) ─────────────────────────────────────────────────


def result_text(records: Sequence[ArtifactRecord], caption: str | None) -> str:
    """What the model reads back after a send."""
    parts = [f"{r.name} ({format_bytes(r.size or 0)})" for r in records]
    if len(parts) == 1:
        text = f"Sent {parts[0]} to the chat."
    else:
        text = f"Sent {len(parts)} files to the chat: {', '.join(parts)}."
    if caption:
        text += f' Caption: "{caption}"'
    return text


# ── Reading a copy back (the chunk route) ──────────────────────────────


def read_chunk(target: Path, offset: int, length: int, expected_size: int) -> bytes:
    """Up to *length* bytes of the copy at *offset*. ``changed`` if it moved."""
    if offset < 0 or not 0 <= length <= CHUNK_MAX:
        raise ArtifactError("bad_range", "Chunk out of range")
    fd = os.open(target, _CHUNK_FLAGS)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != expected_size:
            raise ArtifactError("changed", "The copy changed on disk")
        out = bytearray()
        while len(out) < length:
            at = offset + len(out)
            if hasattr(os, "pread"):
                block = os.pread(fd, length - len(out), at)
            else:  # Windows has no pread
                os.lseek(fd, at, os.SEEK_SET)
                block = os.read(fd, length - len(out))
            if not block:
                break
            out += block
        return bytes(out)
    finally:
        os.close(fd)


# ── Sending ────────────────────────────────────────────────────────────


class SendError(Exception):
    """A refused send. ``code`` is the tool's error code (§1.2)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Source:
    """One file to send, opened and stat'ed before any copy starts."""

    def __init__(self, fd: int, st: os.stat_result, path: Path, rel: str) -> None:
        self.fd = fd
        self.st = st
        self.path = path
        self.rel = rel


def _sweep_parts(bytes_dir: Path) -> None:
    """Remove ``.<aid>.part`` crash leftovers older than an hour."""
    cutoff = time.time() - _PART_MAX_AGE_S
    try:
        entries = list(os.scandir(bytes_dir))
    except OSError:
        return
    for entry in entries:
        if not _PART_NAME.fullmatch(entry.name):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            continue


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class SentFiles:
    """Copies files into a session and indexes them (TD-5143)."""

    def __init__(self, store: ArtifactStore, config: SessionConfig) -> None:
        self._store = store
        self._config = config

    @staticmethod
    def _lock_for(session_id: str) -> threading.Lock:
        with _SESSION_LOCKS_GUARD:
            return _SESSION_LOCKS.setdefault(session_id, threading.Lock())

    def copy_all(
        self,
        session_id: str,
        paths: Sequence[str],
        *,
        workspace: Path,
        caption: str | None = None,
        tool_call_id: str = "",
    ) -> list[ArtifactRecord]:
        """Copy every file in *paths*, or none. Raises ``SendError``."""
        caption = (caption or "").strip() or None
        with self._lock_for(session_id):
            sources = self._open_all(paths, workspace)
            try:
                self._check_room(session_id, sources)
                return self._copy_sources(session_id, sources, caption, tool_call_id)
            finally:
                for source in sources:
                    os.close(source.fd)

    def _open_all(self, paths: Sequence[str], workspace: Path) -> list[_Source]:
        """Re-check, dedupe, open and stat every file before copying any."""
        root = canonical_path(workspace)
        guard = PathGuard(Boundary(workspace_root=workspace))
        sources: list[_Source] = []
        seen: set[Path] = set()
        try:
            for raw in paths:
                try:
                    path = guard.check_read(raw)
                except RefusalError as exc:
                    raise SendError("boundary_refusal", f"Refused: {exc.reason}") from None
                if path in seen:
                    continue
                seen.add(path)
                if path == root:
                    rel = "."
                elif root in path.parents:
                    rel = path.relative_to(root).as_posix()
                else:
                    rel = str(path)
                sources.append(self._open_one(path, rel))
        except BaseException:
            for source in sources:
                os.close(source.fd)
            raise
        return sources

    def _open_one(self, path: Path, rel: str) -> _Source:
        folder = (
            f"{rel} is a folder, not a file. Zip it first (for example with the shell "
            "tool), then send the zip. Nothing was sent."
        )
        special = f"{rel} is not a regular file."
        try:
            # lstat first, so a FIFO or a device is never opened at all.
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):
                raise SendError("not_a_file", folder)
            if not stat.S_ISREG(mode):
                raise SendError("not_a_file", special)
            fd = os.open(path, _READ_FLAGS)
        except FileNotFoundError:
            raise SendError("file_missing", f"No file at {rel}. Nothing was sent.") from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SendError("not_a_file", special) from None
            if path.is_dir():
                raise SendError("not_a_file", folder) from None
            raise SendError(
                "copy_failed", f"Could not copy {rel}: {exc.strerror or exc}. Nothing was sent."
            ) from None
        try:
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                raise SendError("not_a_file", folder)
            if not stat.S_ISREG(st.st_mode):
                raise SendError("not_a_file", special)
            limit = self._config.send_file_max_bytes
            if st.st_size > limit:
                raise SendError(
                    "file_too_large",
                    f"{rel} is {format_bytes(st.st_size)}; the limit is {format_bytes(limit)} "
                    "per file (session.send_file_max_bytes). Nothing was sent.",
                )
        except BaseException:
            os.close(fd)
            raise
        return _Source(fd, st, path, rel)

    def _check_room(self, session_id: str, sources: list[_Source]) -> None:
        total = sum(s.st.st_size for s in sources)
        used = self._store.sent_bytes_total(session_id)
        quota = self._config.send_file_session_max_bytes
        if used + total > quota:
            raise SendError(
                "session_quota",
                f"This session already holds {format_bytes(used)} of sent files; sending "
                f"{format_bytes(total)} more would pass {format_bytes(quota)} "
                "(session.send_file_session_max_bytes). Nothing was sent.",
            )
        bytes_dir = self._store.bytes_dir(session_id)
        bytes_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        free = shutil.disk_usage(bytes_dir).free
        needed = FREE_MARGIN
        for source in sources:
            needed += source.st.st_size
            if free < needed:
                raise SendError("disk_full", self._disk_full(source))

    @staticmethod
    def _disk_full(source: _Source) -> str:
        return (
            f"Not enough free disk space to keep a copy of {source.rel} "
            f"({format_bytes(source.st.st_size)}). Nothing was sent."
        )

    def _copy_sources(
        self,
        session_id: str,
        sources: list[_Source],
        caption: str | None,
        tool_call_id: str,
    ) -> list[ArtifactRecord]:
        bytes_dir = self._store.bytes_dir(session_id)
        _sweep_parts(bytes_dir)
        created: list[Path] = []
        records: list[ArtifactRecord] = []
        current: _Source | None = None
        try:
            for source in sources:
                current = source
                records.append(
                    self._copy_one(session_id, source, bytes_dir, created, caption, tool_call_id)
                )
            self._store.append_records(session_id, records)
        except SendError:
            self._undo(created)
            raise
        except ArtifactError as exc:
            self._undo(created)
            cause = exc.__cause__
            reason = cause.strerror if isinstance(cause, OSError) and cause.strerror else None
            rel = sources[-1].rel if sources else "the files"
            raise SendError(
                "copy_failed",
                f"Could not copy {rel}: {reason or 'the index could not be saved'}. "
                "Nothing was sent.",
            ) from None
        except OSError as exc:
            self._undo(created)
            if exc.errno == errno.ENOSPC and current is not None:
                raise SendError("disk_full", self._disk_full(current)) from None
            rel = current.rel if current is not None else "the files"
            raise SendError(
                "copy_failed", f"Could not copy {rel}: {exc.strerror or exc}. Nothing was sent."
            ) from None
        except BaseException:
            self._undo(created)
            raise
        return records

    def _copy_one(
        self,
        session_id: str,
        source: _Source,
        bytes_dir: Path,
        created: list[Path],
        caption: str | None,
        tool_call_id: str,
    ) -> ArtifactRecord:
        aid = uuid.uuid4().hex
        part = bytes_dir / f".{aid}.part"
        changed = SendError(
            "file_changed",
            f"{source.rel} changed while it was being copied. Wait for the build to finish, "
            "then send it again. Nothing was sent.",
        )
        out = os.open(part, _PART_FLAGS, 0o600)
        created.append(part)
        digest = hashlib.sha256()
        copied = 0
        try:
            while block := os.read(source.fd, _BLOCK):
                copied += len(block)
                if copied > source.st.st_size:
                    raise changed
                digest.update(block)
                _write_all(out, block)
            os.fsync(out)
        finally:
            os.close(out)
        after = os.fstat(source.fd)
        if copied != source.st.st_size or (after.st_size, after.st_mtime_ns) != (
            source.st.st_size,
            source.st.st_mtime_ns,
        ):
            raise changed
        os.chmod(part, 0o600)
        name = safe_file_name(redact_secrets(source.path.name))
        folder = bytes_dir / aid
        os.mkdir(folder, 0o700)
        created.append(folder)
        os.chmod(folder, 0o700)
        os.replace(part, folder / name)
        _fsync_dir(folder)
        return ArtifactRecord(
            id=aid,
            session_id=session_id,
            mime=mime_for(name),
            path=f"artifacts/{aid}",
            title=name,
            location="session",
            kind="sent_file",
            name=name,
            size=copied,
            sha256=digest.hexdigest(),
            caption=caption,
            tool_call_id=tool_call_id,
        )

    @staticmethod
    def _undo(created: list[Path]) -> None:
        """Remove every part file and folder this call made."""
        for path in reversed(created):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
