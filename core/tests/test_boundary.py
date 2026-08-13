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
from tstd.tools.boundary import PathGuard, RefusalError, is_8_3_short_name


def guard(workspace: Path, writable: tuple[str, ...] | None = None) -> PathGuard:
    """A PathGuard over *workspace* with the given writable globs."""
    return PathGuard(Boundary(workspace_root=workspace, writable_patterns=writable or ("**",)))


# ── AC 1: every path resolved to canonical absolute form before any check ─


class TestCanonicalForm:
    def test_relative_path_canonicalized(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        result = g.check_read(str(tmp_path / "sub" / ".." / "a.txt"))
        assert result == (tmp_path / "a.txt").resolve()

    def test_dot_and_dotdot_resolved(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        result = g.canonicalize(f"{tmp_path}/./x/../y")
        assert result == (tmp_path / "y").resolve()

    def test_check_uses_canonical_not_lexical(self, tmp_path: Path) -> None:
        # Lexically inside, canonically outside: must be refused.
        g = guard(tmp_path)
        escape = str(tmp_path / ".." / ".." / "etc" / "passwd")
        assert not (tmp_path / ".." / ".." / "etc").resolve().is_relative_to(tmp_path.resolve())
        with pytest.raises(RefusalError) as ei:
            g.check_read(escape)
        assert ei.value.code == "outside_workspace"


# ── AC 3: traversal blocked ─────────────────────────────────────────────


class TestTraversal:
    def test_dotdot_sequence_write_refused(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        with pytest.raises(RefusalError) as ei:
            g.check_write(str(tmp_path / ".." / "escape.txt"))
        assert ei.value.code == "outside_workspace"

    def test_absolute_path_refused(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        with pytest.raises(RefusalError) as ei:
            g.check_write("/etc/passwd")
        assert ei.value.code == "outside_workspace"

    def test_symlink_to_outside_file_refused(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret")
        (tmp_path / "link.txt").symlink_to(outside)
        g = guard(tmp_path)
        with pytest.raises(RefusalError) as ei:
            g.check_write(tmp_path / "link.txt")
        assert ei.value.code == "outside_workspace"

    def test_symlinked_parent_directory_refused(self, tmp_path: Path) -> None:
        outside_dir = tmp_path.parent / "outside_dir"
        outside_dir.mkdir(exist_ok=True)
        (tmp_path / "sub").symlink_to(outside_dir, target_is_directory=True)
        g = guard(tmp_path)
        # The path is lexically inside; the parent resolves outside.
        with pytest.raises(RefusalError) as ei:
            g.check_write(tmp_path / "sub" / "evil.txt")
        assert ei.value.code == "outside_workspace"

    def test_path_external_only_after_resolution_refused(self, tmp_path: Path) -> None:
        # "paths that become external only after resolution": a deep
        # chain that pops out via .. after passing through symlinks.
        outside_dir = tmp_path.parent / "outside_dir"
        outside_dir.mkdir(exist_ok=True)
        (tmp_path / "hop").symlink_to(outside_dir, target_is_directory=True)
        g = guard(tmp_path)
        with pytest.raises(RefusalError):
            g.check_write(tmp_path / "hop" / ".." / ".." / "etc" / "shadow")

    def test_read_through_symlink_outside_refused(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "secret.txt"
        outside.write_text("secret")
        (tmp_path / "peek").symlink_to(outside)
        g = guard(tmp_path)
        with pytest.raises(RefusalError) as ei:
            g.check_read(tmp_path / "peek")
        assert ei.value.code == "outside_workspace"


# ── AC 3: hardlinks ─────────────────────────────────────────────────────


class TestHardlink:
    def test_hardlink_to_outside_file_write_refused(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("shared inode")
        target = tmp_path / "innocent_name.txt"
        os.link(outside, target)  # target aliases outside.txt's inode
        g = guard(tmp_path)
        with pytest.raises(RefusalError) as ei:
            g.check_write(target)
        assert ei.value.code == "hardlink"

    def test_single_link_file_write_allowed(self, tmp_path: Path) -> None:
        target = tmp_path / "normal.txt"
        target.write_text("x")
        g = guard(tmp_path)
        assert g.check_write(target) == target.resolve()

    def test_hardlink_read_allowed(self, tmp_path: Path) -> None:
        # Reads through hardlinks are not a mutation; only writes are
        # refused (the in-place write would modify the shared inode).
        outside = tmp_path / "outside.txt"
        outside.write_text("shared inode")
        target = tmp_path / "peek.txt"
        os.link(outside, target)
        g = guard(tmp_path)
        assert g.check_read(target) == target.resolve()


# ── AC 2: access outside writable_paths refused ────────────────────────


class TestWritablePaths:
    def test_default_writable_allows_in_workspace(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        assert g.check_write(tmp_path / "src" / "a.py") == (tmp_path / "src" / "a.py").resolve()

    def test_write_outside_restrictive_patterns_refused(self, tmp_path: Path) -> None:
        g = guard(tmp_path, writable=("src/**",))
        with pytest.raises(RefusalError) as ei:
            g.check_write(tmp_path / "lib" / "a.py")
        assert ei.value.code == "outside_writable_paths"

    def test_write_within_restrictive_patterns_allowed(self, tmp_path: Path) -> None:
        g = guard(tmp_path, writable=("src/**",))
        assert g.check_write(tmp_path / "src" / "deep" / "a.py")

    def test_basename_pattern_matches_at_depth(self, tmp_path: Path) -> None:
        g = guard(tmp_path, writable=("*.py",))
        assert g.check_write(tmp_path / "deep" / "a.py")  # slash-less pattern

    def test_refusal_error_is_clear(self, tmp_path: Path) -> None:
        g = guard(tmp_path, writable=("src/**",))
        with pytest.raises(RefusalError) as ei:
            g.check_write(tmp_path / "lib" / "a.py")
        assert "writable" in ei.value.reason.lower()
        assert ei.value.path == (tmp_path / "lib" / "a.py").resolve()


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
    def test_steering_write_refused(self, tmp_path: Path, target: str) -> None:
        g = guard(tmp_path)
        with pytest.raises(RefusalError) as ei:
            g.check_write(tmp_path / target)
        assert ei.value.code == "steering_file"

    def test_steering_write_refused_even_when_writable(self, tmp_path: Path) -> None:
        # writable_patterns never grants steering files (PD §2.4).
        g = guard(tmp_path, writable=("AGENTS.md", ".tst/**", "**"))
        with pytest.raises(RefusalError) as ei:
            g.check_write(tmp_path / "AGENTS.md")
        assert ei.value.code == "steering_file"

    def test_steering_read_allowed(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        assert g.check_read(tmp_path / "AGENTS.md") == (tmp_path / "AGENTS.md").resolve()


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


# ── Reads ───────────────────────────────────────────────────────────────


class TestReads:
    def test_read_outside_workspace_refused(self, tmp_path: Path) -> None:
        g = guard(tmp_path)
        with pytest.raises(RefusalError) as ei:
            g.check_read("/etc/hosts")
        assert ei.value.code == "outside_workspace"

    def test_read_inside_workspace_allowed(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("x")
        g = guard(tmp_path)
        assert g.check_read(target) == target.resolve()

    def test_read_does_not_apply_writable_patterns(self, tmp_path: Path) -> None:
        # writable_paths governs writes; reads anywhere in the workspace
        # are allowed.
        g = guard(tmp_path, writable=("src/**",))
        assert g.check_read(tmp_path / "lib" / "a.py")
