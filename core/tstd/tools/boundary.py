"""Path boundary enforcement (TD-602) — security-critical.

Every path a tool touches is resolved to canonical absolute form before
any check, then refused with a clear error when it would cross the
boundary: outside the workspace, outside ``writable_paths``, a steering
file (prime §2.4), a hardlink write, or a Windows-unsafe path form.

The canonicalization / workspace / writable-glob / steering primitives are
shared with the decision classifier (single source of truth in
``autonomy.classifier``); this module owns the *enforcement* semantics on
top of them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..autonomy.classifier import (
    Boundary,
    canonical_path,
    is_in_workspace,
    is_steering_write,
    path_matches,
    relative_parts,
)

_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
# Windows 8.3 short names: 1-8 base chars, ~digits, optional 1-3 char ext.
_SHORT_NAME_RE = re.compile(r"^[^~.]{1,8}~\d+(\.[^~.]{0,3})?$", re.IGNORECASE)


class RefusalError(Exception):
    """A path operation was refused by the boundary guard.

    Attributes:
        code: Machine-readable refusal reason: ``windows_unsafe``,
            ``steering_file``, ``outside_workspace``,
            ``outside_writable_paths``, or ``hardlink``.
        path: The canonical path that was refused.
        reason: Human-readable explanation for the error the model sees.
    """

    def __init__(self, code: str, path: Path, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.path = path
        self.reason = reason


# ── Windows-specific form detection (pure, fail-closed) ────────────────


def is_drive_relative_or_absolute(p: str) -> bool:
    """Whether *p* starts with a drive letter (``C:foo`` or ``C:\\foo``).

    Drive-relative paths resolve against a drive's current directory on
    Windows and can land anywhere; drive-absolute paths are outside any
    workspace.  Refused fail-closed on every platform.
    """
    return bool(_DRIVE_PREFIX_RE.match(p))


def is_unc_path(p: str) -> bool:
    """Whether *p* is a UNC path (``\\\\server\\share\\...``)."""
    return p.startswith("\\\\")


def is_8_3_short_name(segment: str) -> bool:
    """Whether *segment* looks like a Windows 8.3 short name (``PROGRA~1``).

    Short names alias the real long name on Windows, so a boundary check
    against the short form could silently hit a different path.
    """
    return bool(_SHORT_NAME_RE.match(segment))


def is_alternate_data_stream(p: str) -> bool:
    """Whether *p* names an NTFS alternate data stream (``file:stream``)."""
    return ":" in Path(p).name


def windows_unsafe_reason(p: str) -> str | None:
    """Reason *p* is Windows-unsafe, or ``None`` if it is safe.

    Every Windows-specific form is refused fail-closed on all platforms —
    a workspace may be shared or moved across operating systems.
    """
    if is_drive_relative_or_absolute(p):
        return "drive-letter path (drive-relative or drive-absolute)"
    if is_unc_path(p):
        return "UNC path (\\\\server\\share) — outside any workspace"
    if any(is_8_3_short_name(part) for part in Path(p).parts):
        return "8.3 short name segment (can alias a different path on Windows)"
    if is_alternate_data_stream(p):
        return "alternate data stream (colon in filename)"
    return None


# ── The guard ──────────────────────────────────────────────────────────


class PathGuard:
    """Canonicalizes paths and refuses access outside the boundary.

    Usage::

        guard = PathGuard(Boundary(workspace_root=Path("/work/proj")))
        try:
            target = guard.check_write(raw_path)
        except RefusalError as e:
            # refused — return a clear error to the model

    ``check_read`` enforces the workspace wall; ``check_write`` enforces
    the wall plus ``writable_paths``, steering files, and hardlinks.
    """

    def __init__(self, boundary: Boundary) -> None:
        self.boundary = boundary

    def canonicalize(self, raw: str | Path) -> Path:
        """Resolve *raw* to absolute canonical form (symlinks resolved)."""
        return canonical_path(Path(raw))

    def check_read(self, raw: str | Path) -> Path:
        """Check a read target; returns its canonical path.

        Raises:
            RefusalError: If the path form is Windows-unsafe or the
                canonical path lies outside the workspace.
        """
        target = self.canonicalize(raw)
        unsafe = windows_unsafe_reason(str(raw))
        if unsafe:
            raise RefusalError("windows_unsafe", target, unsafe)
        if not is_in_workspace(self.boundary, target):
            raise RefusalError(
                "outside_workspace",
                target,
                f"path outside the workspace: {target}",
            )
        return target

    def check_write(self, raw: str | Path) -> Path:
        """Check a write target; returns its canonical path.

        Refuses (in order): Windows-unsafe forms, steering files
        (unconditionally, prime §2.4), paths outside the workspace,
        paths outside ``writable_paths``, and in-place writes to
        hardlinked files.

        Raises:
            RefusalError: With a machine-readable ``code`` and a clear
                ``reason`` for the model.
        """
        target = self.canonicalize(raw)
        unsafe = windows_unsafe_reason(str(raw))
        if unsafe:
            raise RefusalError("windows_unsafe", target, unsafe)
        if is_steering_write(self.boundary, target):
            raise RefusalError(
                "steering_file",
                target,
                "steering files (AGENTS.md/CLAUDE.md/.tst/rules) are read-only",
            )
        root = self.boundary.workspace_root
        if root is None or not is_in_workspace(self.boundary, target):
            raise RefusalError(
                "outside_workspace",
                target,
                f"path outside the workspace: {target}",
            )
        rel = relative_parts(target, root)
        if not any(path_matches(pattern, rel) for pattern in self.boundary.writable_patterns):
            raise RefusalError(
                "outside_writable_paths",
                target,
                f"path outside writable_paths: {target}",
            )
        if self._is_hardlinked(target):
            raise RefusalError(
                "hardlink",
                target,
                "refusing in-place write to a hardlinked file; use an atomic replace instead",
            )
        return target

    def _is_hardlinked(self, target: Path) -> bool:
        """Whether *target* already exists and is hardlinked (nlink > 1).

        A hardlink inside the workspace can alias a file outside it; an
        in-place write would modify the shared inode.  Fresh files (no
        stat) are not hardlinks.
        """
        try:
            return os.stat(target).st_nlink > 1
        except OSError:
            return False
