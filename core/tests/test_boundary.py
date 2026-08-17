"""Path boundary enforcement attacks — TD-602 (security-critical).

Tests-first per AGENTS.md §7: every vector is an attack test that must
pass against the guard before the guard exists.  Covers the TD-602
acceptance criteria: canonical form before checks, writable-path refusal,
traversal (``../``, absolute, symlinks, hardlinks, post-resolution
external paths), Windows-specific forms, and unconditional steering-file
refusal.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tstd.autonomy import Boundary
from tstd.autonomy.classifier import canonical_path
from tstd.tools.boundary import (
    PathGuard,
    RefusalError,
    is_8_3_short_name,
    windows_unsafe_reason,
)


def guard(workspace: Path, writable: tuple[str, ...] | None = None) -> PathGuard:
    """A PathGuard over *workspace* with the given writable globs."""
    return PathGuard(Boundary(workspace_root=workspace, writable_patterns=writable or ("**",)))


@pytest.fixture
def ws_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Chdir into the workspace so guard inputs can be workspace-relative.

    Relative inputs exercise the real boundary semantics (traversal,
    symlinks, hardlinks, steering, writable globs) identically on every
    platform, with no path-form question in the way.  Absolute inputs
    would be judged differently per host: off Windows a drive-letter path
    is refused for being unresolvable, and the two forms only converged on
    Windows itself in TD-1406.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


def canonical(rel: str) -> Path:
    """The canonical target the guard must return for a relative input."""
    return canonical_path(Path(rel))


# ── AC 1: every path resolved to canonical absolute form before any check ─


class TestCanonicalForm:
    def test_relative_path_canonicalized(self, ws_cwd: Path) -> None:
        g = guard(ws_cwd)
        result = g.check_read("sub/../a.txt")
        assert result == canonical("a.txt")

    def test_dot_and_dotdot_resolved(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        result = g.canonicalize(f"{tmp_path}/./x/../y")
        assert result == (tmp_path / "y").resolve()

    def test_check_uses_canonical_not_lexical(self, ws_cwd: Path) -> None:
        # Lexically inside, canonically outside: must be refused.
        g = guard(ws_cwd)
        escape = "../../etc/passwd"
        assert not (ws_cwd / ".." / ".." / "etc").resolve().is_relative_to(ws_cwd.resolve())
        with pytest.raises(RefusalError) as ei:
            g.check_read(escape)
        assert ei.value.code == "outside_workspace"


# ── AC 3: traversal blocked ─────────────────────────────────────────────


class TestTraversal:
    def test_dotdot_sequence_write_refused(self, ws_cwd: Path) -> None:
        g = guard(ws_cwd)
        with pytest.raises(RefusalError) as ei:
            g.check_write("../escape.txt")
        assert ei.value.code == "outside_workspace"

    def test_absolute_path_refused(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        with pytest.raises(RefusalError) as ei:
            g.check_write("/etc/passwd")
        assert ei.value.code == "outside_workspace"

    def test_symlink_to_outside_file_refused(self, ws_cwd: Path) -> None:
        outside = ws_cwd.parent / "outside.txt"
        outside.write_text("secret")
        (ws_cwd / "link.txt").symlink_to(outside)
        g = guard(ws_cwd)
        with pytest.raises(RefusalError) as ei:
            g.check_write("link.txt")
        assert ei.value.code == "outside_workspace"

    def test_symlinked_parent_directory_refused(self, ws_cwd: Path) -> None:
        outside_dir = ws_cwd.parent / "outside_dir"
        outside_dir.mkdir(exist_ok=True)
        (ws_cwd / "sub").symlink_to(outside_dir, target_is_directory=True)
        g = guard(ws_cwd)
        # The path is lexically inside; the parent resolves outside.
        with pytest.raises(RefusalError) as ei:
            g.check_write("sub/evil.txt")
        assert ei.value.code == "outside_workspace"

    def test_path_external_only_after_resolution_refused(self, ws_cwd: Path) -> None:
        # "paths that become external only after resolution": a deep
        # chain that pops out via .. after passing through symlinks.
        outside_dir = ws_cwd.parent / "outside_dir"
        outside_dir.mkdir(exist_ok=True)
        (ws_cwd / "hop").symlink_to(outside_dir, target_is_directory=True)
        g = guard(ws_cwd)
        with pytest.raises(RefusalError):
            g.check_write("hop/../../etc/shadow")

    def test_read_through_symlink_outside_refused(self, ws_cwd: Path) -> None:
        outside = ws_cwd.parent / "secret.txt"
        outside.write_text("secret")
        (ws_cwd / "peek").symlink_to(outside)
        g = guard(ws_cwd)
        with pytest.raises(RefusalError) as ei:
            g.check_read("peek")
        assert ei.value.code == "outside_workspace"


# ── AC 3: hardlinks ─────────────────────────────────────────────────────


class TestHardlink:
    def test_hardlink_to_outside_file_write_refused(self, ws_cwd: Path) -> None:
        outside = ws_cwd.parent / "outside.txt"
        outside.write_text("shared inode")
        target = ws_cwd / "innocent_name.txt"
        os.link(outside, target)  # target aliases outside.txt's inode
        g = guard(ws_cwd)
        with pytest.raises(RefusalError) as ei:
            g.check_write("innocent_name.txt")
        assert ei.value.code == "hardlink"

    def test_single_link_file_write_allowed(self, ws_cwd: Path) -> None:
        target = ws_cwd / "normal.txt"
        target.write_text("x")
        g = guard(ws_cwd)
        assert g.check_write("normal.txt") == canonical("normal.txt")

    def test_hardlink_read_allowed(self, ws_cwd: Path) -> None:
        # Reads through hardlinks are not a mutation; only writes are
        # refused (the in-place write would modify the shared inode).
        outside = ws_cwd / "outside.txt"
        outside.write_text("shared inode")
        target = ws_cwd / "peek.txt"
        os.link(outside, target)
        g = guard(ws_cwd)
        assert g.check_read("peek.txt") == canonical("peek.txt")


# ── AC 2: access outside writable_paths refused ────────────────────────


class TestWritablePaths:
    def test_default_writable_allows_in_workspace(self, ws_cwd: Path) -> None:
        g = guard(ws_cwd)
        assert g.check_write("src/a.py") == canonical("src/a.py")

    def test_write_outside_restrictive_patterns_refused(self, ws_cwd: Path) -> None:
        g = guard(ws_cwd, writable=("src/**",))
        with pytest.raises(RefusalError) as ei:
            g.check_write("lib/a.py")
        assert ei.value.code == "outside_writable_paths"

    def test_write_within_restrictive_patterns_allowed(self, ws_cwd: Path) -> None:
        g = guard(ws_cwd, writable=("src/**",))
        assert g.check_write("src/deep/a.py")

    def test_basename_pattern_matches_at_depth(self, ws_cwd: Path) -> None:
        g = guard(ws_cwd, writable=("*.py",))
        assert g.check_write("deep/a.py")  # slash-less pattern

    def test_refusal_error_is_clear(self, ws_cwd: Path) -> None:
        g = guard(ws_cwd, writable=("src/**",))
        with pytest.raises(RefusalError) as ei:
            g.check_write("lib/a.py")
        assert "writable" in ei.value.reason.lower()
        assert ei.value.path == canonical("lib/a.py")


# ── AC 5: steering-file writes refused unconditionally ──────────────────


class TestSteeringFiles:
    @pytest.mark.parametrize(
        "target",
        [
            "AGENTS.md",
            "CLAUDE.md",
            "src/AGENTS.md",  # nested steering file
            "src/CLAUDE.md",
            ".tst/rules/custom.md",
            ".tst/rules/nested/deep.md",
        ],
    )
    def test_steering_write_refused(self, ws_cwd: Path, target: str) -> None:
        g = guard(ws_cwd)
        with pytest.raises(RefusalError) as ei:
            g.check_write(target)
        assert ei.value.code == "steering_file"

    def test_steering_write_refused_even_when_writable(self, ws_cwd: Path) -> None:
        # writable_patterns never grants steering files (PD §2.4).
        g = guard(ws_cwd, writable=("AGENTS.md", ".tst/**", "**"))
        with pytest.raises(RefusalError) as ei:
            g.check_write("AGENTS.md")
        assert ei.value.code == "steering_file"

    def test_steering_read_allowed(self, ws_cwd: Path) -> None:
        g = guard(ws_cwd)
        assert g.check_read("AGENTS.md") == canonical("AGENTS.md")


# ── AC 4: Windows-specific forms ────────────────────────────────────────


class TestWindowsForms:
    def test_drive_relative_refused(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        with pytest.raises(RefusalError) as ei:
            g.check_write("C:foo")
        assert ei.value.code == "windows_unsafe"

    def test_drive_absolute_refused(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        with pytest.raises(RefusalError) as ei:
            g.check_write(r"C:\Windows\system32\evil.dll")
        if sys.platform == "win32":
            # TD-1406: drive-absolute is the native absolute form on Windows
            # — canonicalized and refused for crossing the workspace wall.
            assert ei.value.code == "outside_workspace"
        else:
            assert ei.value.code == "windows_unsafe"

    def test_unc_path_refused(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        with pytest.raises(RefusalError) as ei:
            g.check_write(r"\\server\share\evil.txt")
        assert ei.value.code == "windows_unsafe"

    def test_8_3_short_name_refused(self, tmp_path: Path) -> None:
        assert is_8_3_short_name("PROGRA~1")
        assert is_8_3_short_name("DOCUME~1.TXT")
        assert not is_8_3_short_name("documents")
        g = guard(tmp_path)
        with pytest.raises(RefusalError) as ei:
            g.check_write(str(tmp_path / "PROGRA~1" / "x.dll"))
        assert ei.value.code == "windows_unsafe"

    def test_alternate_data_stream_refused(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        # NTFS ADS marker: a colon in the basename ("file.txt:stream").
        with pytest.raises(RefusalError) as ei:
            g.check_write(str(tmp_path / "note.txt:ads"))
        assert ei.value.code == "windows_unsafe"

    def test_fail_closed_on_posix_too(self, tmp_path: Path) -> None:
        # Windows-unsafe forms are refused on every platform, not just
        # Windows — a workspace may be shared across OSes.
        g = guard(tmp_path)
        with pytest.raises(RefusalError):
            g.check_read(r"\\server\share\evil.txt")


# ── TD-1406: 8.3 short names are a form problem only where they alias ───


class TestShortNameCanonicalization:
    """The 8.3 check reads the canonical path on Windows, the raw one else.

    Windows canonicalization (``GetFinalPathNameByHandle``, reached through
    ``os.path.realpath``) returns the long form, so a short-named *ancestor*
    — ``C:\\Users\\RUNNER~1\\...``, which is where a CI runner's temp
    directory lives — carries no aliasing risk once resolved.  No other
    filesystem knows the short→long mapping, so off Windows the fail-closed
    refusal stands.

    ``sys.platform`` is patched rather than skipped so both branches are
    proven on every host: the win32 branch must not be a claim only the
    Windows leg can check.  The patch does not change ``pathlib``'s
    flavour, so the vectors use forward slashes — legal on Windows, and
    parsed into segments by ``PosixPath`` too.  Backslash vectors would
    read as one segment off Windows and prove nothing.  ``TestWindowsNative``
    carries the end-to-end check that only a real host can make.
    """

    # A short-named ancestor that canonicalization expanded away.  Drive
    # letters are left off the shared vectors so the 8.3 rule is the only
    # one in play on both branches — off Windows a drive letter is refused
    # first, and would mask what these assertions are about.
    EXPANDED_RAW = "/Users/RUNNER~1/AppData/Local/Temp/ws/a.txt"
    EXPANDED_CANONICAL = Path("/Users/runneradmin/AppData/Local/Temp/ws/a.txt")

    # A short name that survived canonicalization — still an alias risk.
    SURVIVING = "/ws/PROGRA~1/x.dll"

    def test_expanded_short_name_allowed_on_win32(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert windows_unsafe_reason(self.EXPANDED_RAW, self.EXPANDED_CANONICAL) is None

    def test_drive_absolute_runner_temp_path_allowed_on_win32(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The real collateral this fixes: pytest's tmp_path on a Windows
        # runner is a drive-absolute path under a short-named ancestor.
        monkeypatch.setattr(sys, "platform", "win32")
        raw = "C:/Users/RUNNER~1/AppData/Local/Temp/pytest-of-runneradmin/ws/a.txt"
        canonical = Path("C:/Users/runneradmin/AppData/Local/Temp/pytest-of-runneradmin/ws/a.txt")
        assert windows_unsafe_reason(raw, canonical) is None

    def test_surviving_short_name_refused_on_win32(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The expansion is what removes the risk; a segment the filesystem
        # did not expand is still refused on Windows.
        monkeypatch.setattr(sys, "platform", "win32")
        reason = windows_unsafe_reason(self.SURVIVING, Path(self.SURVIVING))
        assert reason is not None
        assert "8.3" in reason

    def test_expanded_short_name_still_refused_off_win32(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Off Windows nothing knows the mapping, so the canonical form is
        # not evidence of anything — the refusal is unchanged.
        monkeypatch.setattr(sys, "platform", "linux")
        reason = windows_unsafe_reason(self.EXPANDED_RAW, self.EXPANDED_CANONICAL)
        assert reason is not None
        assert "8.3" in reason

    def test_omitted_canonical_form_is_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Callers that hold no canonical path (the classifier) get the
        # conservative answer for a short name no filesystem resolved.
        monkeypatch.setattr(sys, "platform", "win32")
        assert windows_unsafe_reason(self.SURVIVING) is not None

    def test_other_unsafe_forms_ignore_the_canonical_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only the 8.3 rule consults the canonical form; UNC and ADS stay
        # raw-string judgements on every platform.
        monkeypatch.setattr(sys, "platform", "win32")
        assert windows_unsafe_reason(r"\\server\share\x.txt", Path("/ws/x.txt")) is not None
        assert windows_unsafe_reason("/ws/note.txt:ads", Path("/ws/note.txt")) is not None
        # Drive-relative stays refused on Windows too — TD-1406 legalised
        # only the drive-*absolute* form.
        assert windows_unsafe_reason("C:note.txt", Path("/ws/note.txt")) is not None


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows semantics")
class TestWindowsNative:
    """Native Windows-only integration checks (run on the Windows CI runner)."""

    def test_absolute_win_path_refused(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        with pytest.raises(RefusalError):
            g.check_write(r"C:\Windows\system32\drivers\etc\hosts")

    def test_unc_refused(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        with pytest.raises(RefusalError):
            g.check_write(r"\\localhost\C$\Windows\evil.txt")

    def test_drive_absolute_in_workspace_allowed(self, tmp_path: Path) -> None:
        # TD-1406 criterion 1, end to end: the native absolute form of an
        # in-workspace path is usable.  On a CI runner tmp_path also sits
        # under a short-named ancestor (RUNNER~1), so this is criterion 2's
        # end-to-end check too — the one no POSIX host can make.
        target = tmp_path / "src" / "a.txt"
        target.parent.mkdir(parents=True)
        target.write_text("x")
        g = guard(tmp_path)
        assert g.check_write(str(target)) == canonical_path(target)
        assert g.check_read(str(target)) == canonical_path(target)

    def test_short_name_outside_workspace_still_refused(self, tmp_path: Path) -> None:
        # Expansion removes the form problem, not the wall: a short-named
        # path that resolves outside the workspace is still refused.
        g = guard(tmp_path)
        with pytest.raises(RefusalError) as ei:
            g.check_read(r"C:\PROGRA~1\evil.dll")
        assert ei.value.code == "outside_workspace"


# ── Reads ───────────────────────────────────────────────────────────────


class TestReads:
    def test_read_outside_workspace_refused(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        with pytest.raises(RefusalError) as ei:
            g.check_read("/etc/hosts")
        assert ei.value.code == "outside_workspace"

    def test_read_inside_workspace_allowed(self, ws_cwd: Path) -> None:
        target = ws_cwd / "a.txt"
        target.write_text("x")
        g = guard(ws_cwd)
        assert g.check_read("a.txt") == canonical("a.txt")

    def test_read_does_not_apply_writable_patterns(self, ws_cwd: Path) -> None:
        # writable_paths governs writes; reads anywhere in the workspace
        # are allowed.
        g = guard(ws_cwd, writable=("src/**",))
        assert g.check_read("lib/a.py")
