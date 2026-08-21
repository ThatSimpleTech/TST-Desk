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
import sys
from pathlib import Path

from ..autonomy.classifier import (
    Boundary,
    canonical_path,
    is_in_workspace,
    is_memory_write,
    is_steering_write,
    path_matches,
    relative_parts,
)

_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_DRIVE_ABS_RE = re.compile(r"^[A-Za-z]:[/\\]")
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
    Windows and can land anywhere; drive-letter paths cannot be resolved
    at all off Windows.  Refused fail-closed on every platform except the
    drive-absolute form on a Windows host (see ``windows_unsafe_reason``).
    """
    return bool(_DRIVE_PREFIX_RE.match(p))


def is_drive_absolute(p: str) -> bool:
    """Whether *p* is a drive-absolute path (``C:\\foo`` or ``C:/foo``).

    The native absolute path form on Windows: on a Windows host it
    canonicalizes like any absolute path and faces the normal workspace
    checks.  Off Windows it cannot be resolved and stays refused.
    """
    return bool(_DRIVE_ABS_RE.match(p))


def is_unc_path(p: str) -> bool:
    """Whether *p* is a UNC path (``\\\\server\\share\\...``)."""
    return p.startswith("\\\\")


def is_8_3_short_name(segment: str) -> bool:
    """Whether *segment* looks like a Windows 8.3 short name (``PROGRA~1``).

    Short names alias the real long name on Windows, so a boundary check
    against the short form could silently hit a different path.
    """
    return bool(_SHORT_NAME_RE.match(segment))


def _short_name_target(p: str, canonical: Path | None) -> Path:
    """The path the 8.3 rule judges: canonical on Windows, raw elsewhere.

    Windows canonicalization (``GetFinalPathNameByHandle``, reached through
    ``os.path.realpath``) returns the long form, so a short name that named
    a real path is already gone by the time the guard checks containment —
    there is nothing left to alias.  Any segment that *survives* resolution
    named nothing the filesystem could expand, and stays refused.

    Off Windows no filesystem knows the short→long mapping, so the raw
    string is all there is and the refusal stays fail-closed (TD-1406).
    """
    if sys.platform != "win32":
        return Path(p)
    return canonical if canonical is not None else canonical_path(Path(p))


def is_alternate_data_stream(p: str) -> bool:
    """Whether *p* names an NTFS alternate data stream (``file:stream``)."""
    return ":" in Path(p).name


def windows_unsafe_reason(p: str, canonical: Path | None = None) -> str | None:
    """Reason *p* is Windows-unsafe, or ``None`` if it is safe.

    Ambiguous and unresolvable forms are refused fail-closed on all
    platforms — a workspace may be shared or moved across operating
    systems.  Two forms are platform-conditional (TD-1406), both because
    Windows is the only host that can resolve them:

    - A **drive-absolute** path is the native absolute form on Windows, so
      on a Windows host it is not a form problem — it canonicalizes and
      faces the normal workspace / writable-paths checks below.  Refusing
      it there would make every path tool unusable, since absolute Windows
      paths always carry a drive letter.
    - An **8.3 short name** aliases a long name only until the path is
      resolved, and on Windows resolution expands it.  See
      ``_short_name_target``.

    Args:
        p: The raw path string, as the caller supplied it.
        canonical: Its canonical form, when the caller already holds one.
            Only the 8.3 rule reads it, and only on Windows.  Omitting it
            costs a resolve on a Windows host and changes nothing
            elsewhere; passing a canonical form never loosens any other
            rule.
    """
    if is_drive_relative_or_absolute(p):
        if sys.platform != "win32":
            return "drive-letter path (cannot be resolved on this platform)"
        if not is_drive_absolute(p):
            return "drive-relative path (resolves against a drive's current directory)"
    if is_unc_path(p):
        return "UNC path (\\\\server\\share) — outside any workspace"
    if any(is_8_3_short_name(part) for part in _short_name_target(p, canonical).parts):
        return "8.3 short name segment (can alias a different path on Windows)"
    if is_alternate_data_stream(p):
        return "alternate data stream (colon in filename)"
    # Trailing dot or space in a component (TD-4820): Win32 strips both at
    # open time, so `AGENTS.md.` or `.tst./config.yaml` alias the real
    # steering file on Windows while creating an inert lookalike elsewhere.
    # Refused everywhere — the guard is fail-closed on any shipped
    # platform's unsafe forms.  "." and ".." are navigation, not components.
    if any(part not in (".", "..") and part.endswith((".", " ")) for part in Path(p).parts):
        return "trailing dot or space in a path component (Windows strips it at open)"
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
        """Resolve *raw* to absolute canonical form (symlinks resolved).

        A relative path is joined to the workspace root first, not to the
        process cwd. The packaged sidecar's cwd is ``/``, so joining there
        turned ``docs/foo.md`` into ``/docs/foo.md`` and refused a file
        that was inside the workspace (TD-608).
        """
        path = Path(raw)
        root = self.boundary.workspace_root
        if not path.is_absolute() and root is not None:
            path = root / path
        return canonical_path(path)

    def check_read(self, raw: str | Path) -> Path:
        """Check a read target; returns its canonical path.

        Raises:
            RefusalError: If the path form is Windows-unsafe or the
                canonical path lies outside the workspace.
        """
        target = self.canonicalize(raw)
        unsafe = windows_unsafe_reason(str(raw), target)
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
        unsafe = windows_unsafe_reason(str(raw), target)
        if unsafe:
            raise RefusalError("windows_unsafe", target, unsafe)
        if is_steering_write(self.boundary, target):
            raise RefusalError(
                "steering_file",
                target,
                "steering files (AGENTS.md/CLAUDE.md/.tst/rules) and the "
                "approval policy (.tst/config.yaml) are read-only",
            )
        root = self.boundary.workspace_root
        if root is None or not is_in_workspace(self.boundary, target):
            raise RefusalError(
                "outside_workspace",
                target,
                f"path outside the workspace: {target}",
            )
        rel = relative_parts(target, root)
        # Spec §5 / TD-2102: memory writes are allowed even when the
        # workspace wall's writable_paths would otherwise exclude them.
        if not is_memory_write(self.boundary, target) and not any(
            path_matches(pattern, rel) for pattern in self.boundary.writable_patterns
        ):
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
