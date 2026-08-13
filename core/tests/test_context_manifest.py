"""Tests for workspace manifest generation (TD-507).

Covers: basic generation, git vs walk fallback, depth/entry/byte caps,
truncation markers, caching with debounce, ignore patterns, and the
large-repository performance guarantee.
"""

from __future__ import annotations

import time
from pathlib import Path

from tstd.context.manifest import (
    ManifestConfig,
    ManifestResult,
    WorkspaceManifest,
)


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _build_workspace(base: Path, *, files: list[str] | None = None) -> Path:
    """Create a workspace with *files* (relative paths)."""
    ws = base / "workspace"
    if files:
        for f in files:
            _write(ws / f)
    return ws


# ── Tests: basic generation ──────────────────────────────────────────────


class TestBasicGeneration:
    """Manifest is generated from a workspace."""

    def test_empty_workspace(self, tmp_path: Path) -> None:
        ws = _build_workspace(tmp_path)
        manifest = WorkspaceManifest()
        result = manifest.build(ws)
        assert result.entry_count == 0
        assert result.truncated is False
        assert "Workspace files" in result.text

    def test_single_file(self, tmp_path: Path) -> None:
        ws = _build_workspace(tmp_path, files=["main.py"])
        result = WorkspaceManifest().build(ws)
        assert result.entry_count == 1
        assert "main.py" in result.text

    def test_multiple_files(self, tmp_path: Path) -> None:
        ws = _build_workspace(
            tmp_path,
            files=["main.py", "src/api/routes.py", "README.md"],
        )
        result = WorkspaceManifest().build(ws)
        assert result.entry_count == 3
        assert "main.py" in result.text
        assert "src/api/routes.py" in result.text
        assert "README.md" in result.text

    def test_files_sorted(self, tmp_path: Path) -> None:
        ws = _build_workspace(tmp_path, files=["z.py", "a.py", "m.py"])
        result = WorkspaceManifest().build(ws)
        lines = result.text.splitlines()
        # Header + 3 files
        assert len(lines) == 4
        assert lines[1] == "a.py"
        assert lines[2] == "m.py"
        assert lines[3] == "z.py"


# ── Tests: depth cap ─────────────────────────────────────────────────────


class TestDepthCap:
    """Files deeper than max_depth are excluded."""

    def test_default_depth_excludes_deep(self, tmp_path: Path) -> None:
        """Default max_depth=8. A file at depth 9 should be excluded."""
        ws = _build_workspace(
            tmp_path,
            files=[
                "src/main.py",  # depth 1
                "a/b/c/d/e/f/g/h/i/deep.py",  # depth 9 → excluded
            ],
        )
        result = WorkspaceManifest().build(ws)
        # With default depth 8, a file at depth 9 (10 slashes? No, 9 slashes → 10 components → > 8)
        # "a/b/c/d/e/f/g/h/i/deep.py" has 9 slashes, 10 components → 10 > 8 → excluded
        assert "deep.py" not in result.text
        assert "src/main.py" in result.text

    def test_custom_depth(self, tmp_path: Path) -> None:
        config = ManifestConfig(max_depth=2)
        ws = _build_workspace(
            tmp_path,
            files=["a.py", "src/b.py", "src/api/c.py"],  # depth 0, 1, 2
        )
        # "src/api/c.py" has 2 slashes, 3 components → 3 > 2 → excluded
        result = WorkspaceManifest(config=config).build(ws)
        assert "a.py" in result.text
        assert "src/b.py" in result.text
        assert "src/api/c.py" not in result.text


# ── Tests: entry cap ─────────────────────────────────────────────────────


class TestEntryCap:
    """Manifest is truncated at max_entries."""

    def test_under_cap(self, tmp_path: Path) -> None:
        config = ManifestConfig(max_entries=10)
        ws = _build_workspace(tmp_path, files=[f"file_{i}.py" for i in range(5)])
        result = WorkspaceManifest(config=config).build(ws)
        assert result.entry_count == 5
        assert result.truncated is False

    def test_at_cap(self, tmp_path: Path) -> None:
        config = ManifestConfig(max_entries=5)
        ws = _build_workspace(tmp_path, files=[f"file_{i}.py" for i in range(5)])
        result = WorkspaceManifest(config=config).build(ws)
        assert result.entry_count == 5
        assert result.truncated is False

    def test_over_cap(self, tmp_path: Path) -> None:
        config = ManifestConfig(max_entries=5)
        ws = _build_workspace(tmp_path, files=[f"file_{i}.py" for i in range(10)])
        result = WorkspaceManifest(config=config).build(ws)
        assert result.entry_count == 5
        assert result.truncated is True
        assert "truncated" in result.text

    def test_truncation_marker(self, tmp_path: Path) -> None:
        config = ManifestConfig(max_entries=3)
        ws = _build_workspace(tmp_path, files=[f"file_{i}.py" for i in range(10)])
        result = WorkspaceManifest(config=config).build(ws)
        assert "<!-- ... truncated at 3 entries" in result.text


# ── Tests: git-based generation ──────────────────────────────────────────


class TestGitGeneration:
    """Manifest uses git ls-files when available."""

    def test_git_repo_uses_git(self, tmp_path: Path) -> None:
        ws = _build_workspace(tmp_path, files=["main.py", "README.md"])
        # Init a git repo with a commit
        import subprocess

        subprocess.run(["git", "init"], cwd=ws, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@test.com", "add", "."],
            cwd=ws,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@test.com",
                "commit",
                "-m",
                "init",
            ],
            cwd=ws,
            capture_output=True,
        )
        # Add an untracked file
        _write(ws / "untracked.py")

        result = WorkspaceManifest().build(ws)
        # Git mode should include tracked + untracked files
        assert "main.py" in result.text
        assert "README.md" in result.text
        assert "untracked.py" in result.text

    def test_git_ignored_files_excluded(self, tmp_path: Path) -> None:
        ws = _build_workspace(tmp_path, files=["main.py"])
        _write(ws / ".gitignore", "*.log\n")
        _write(ws / "debug.log", "ignored")
        _write(ws / "important.log", "also ignored")
        import subprocess

        subprocess.run(["git", "init"], cwd=ws, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@test.com", "add", "."],
            cwd=ws,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@test.com",
                "commit",
                "-m",
                "init",
            ],
            cwd=ws,
            capture_output=True,
        )
        result = WorkspaceManifest().build(ws)
        assert "main.py" in result.text
        assert "debug.log" not in result.text
        assert "important.log" not in result.text


# ── Tests: fallback ignore patterns ──────────────────────────────────────


class TestFallbackIgnore:
    """Walk fallback respects default and configurable ignore patterns."""

    def test_default_ignore_excludes_node_modules(self, tmp_path: Path) -> None:
        ws = _build_workspace(tmp_path, files=["src/main.py", "node_modules/pkg/index.js"])
        result = WorkspaceManifest().build(ws)
        assert "src/main.py" in result.text
        assert "node_modules" not in result.text

    def test_custom_ignore_pattern(self, tmp_path: Path) -> None:
        config = ManifestConfig(ignore_patterns=frozenset({"build", "dist"}))
        ws = _build_workspace(
            tmp_path,
            files=["src/main.py", "build/output.o", "dist/bundle.js"],
        )
        result = WorkspaceManifest(config=config).build(ws)
        assert "src/main.py" in result.text
        assert "build" not in result.text
        assert "dist" not in result.text


# ── Tests: caching and debounce ──────────────────────────────────────────


class TestCache:
    """Manifest is cached with a debounce period."""

    def test_cached_result_returned(self, tmp_path: Path) -> None:
        ws = _build_workspace(tmp_path, files=["a.py"])
        manifest = WorkspaceManifest()
        r1 = manifest.build(ws)
        r2 = manifest.build(ws)
        # Same result object (identity check — cache hit)
        assert r1 is r2

    def test_invalidate_forces_rebuild(self, tmp_path: Path) -> None:
        ws = _build_workspace(tmp_path, files=["a.py"])
        manifest = WorkspaceManifest()
        r1 = manifest.build(ws)
        manifest.invalidate(ws)
        _write(ws / "b.py")
        r2 = manifest.build(ws)
        assert r2 is not r1
        assert r2.entry_count == 2

    def test_cache_expires_after_debounce(self, tmp_path: Path) -> None:
        ws = _build_workspace(tmp_path, files=["a.py"])
        manifest = WorkspaceManifest()
        from tstd.context.manifest import _DEBOUNCE_SECONDS

        r1 = manifest.build(ws)
        # Simulate time passing beyond the debounce
        cache_key = str(ws.resolve())
        # Manually age the cache entry
        manifest._cache[cache_key] = (time.time() - _DEBOUNCE_SECONDS - 1, r1)
        _write(ws / "b.py")
        r2 = manifest.build(ws)
        assert r2 is not r1
        assert r2.entry_count == 2


# ── Tests: large repository performance ──────────────────────────────────


class TestLargeRepo:
    """Large repos (100k+ files) do not stall and respect the cap."""

    def test_large_repo_stops_at_cap(self, tmp_path: Path) -> None:
        """A synthetic repo with 100k+ files stops at the entry cap."""
        config = ManifestConfig(max_entries=5_000)
        ws = _build_workspace(tmp_path)
        # Create 100k files across 100 directories
        for i in range(100):
            for j in range(1000):
                _write(ws / f"dir_{i}" / f"file_{j}.py")
        manifest = WorkspaceManifest(config=config)
        import time as time_mod

        start = time_mod.time()
        result = manifest.build(ws)
        elapsed = time_mod.time() - start
        # Must complete quickly and respect the cap
        assert elapsed < 5.0, f"took {elapsed:.2f}s"
        assert result.entry_count == 5_000
        assert result.truncated is True

    def test_large_git_repo_honors_cap(self, tmp_path: Path) -> None:
        """A synthetic git repo with many files stops at the cap."""
        import subprocess
        import time as time_mod

        config = ManifestConfig(max_entries=1_000)
        ws = _build_workspace(tmp_path)
        # Create 10k files in a git repo
        for i in range(100):
            (ws / f"dir_{i}").mkdir(parents=True, exist_ok=True)
            for j in range(100):
                _write(ws / f"dir_{i}" / f"file_{j}.py")
        subprocess.run(["git", "init"], cwd=ws, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@test.com", "add", "."],
            cwd=ws,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@test.com",
                "commit",
                "-m",
                "init",
            ],
            cwd=ws,
            capture_output=True,
        )
        manifest = WorkspaceManifest(config=config)
        start = time_mod.time()
        result = manifest.build(ws)
        elapsed = time_mod.time() - start
        assert elapsed < 5.0, f"took {elapsed:.2f}s"
        assert result.entry_count == 1_000
        assert result.truncated is True


# ── Tests: non-repo workspace ────────────────────────────────────────────


class TestNonRepoWorkspace:
    """Non-git workspaces use the walk fallback."""

    def test_non_repo_walk_fallback(self, tmp_path: Path) -> None:
        ws = _build_workspace(tmp_path, files=["a.py", "b.py"])
        result = WorkspaceManifest().build(ws)
        assert result.entry_count == 2

    def test_non_repo_ignores_default_dirs(self, tmp_path: Path) -> None:
        ws = _build_workspace(
            tmp_path,
            files=["main.py", ".git/hooks/pre-commit", "__pycache__/cache.py"],
        )
        result = WorkspaceManifest().build(ws)
        assert "main.py" in result.text
        assert ".git" not in result.text
        assert "__pycache__" not in result.text


# ── Tests: ManifestResult ─────────────────────────────────────────────────


class TestManifestResult:
    def test_result_attributes(self, tmp_path: Path) -> None:
        ws = _build_workspace(tmp_path, files=["a.py"])
        result = WorkspaceManifest().build(ws)
        assert isinstance(result, ManifestResult)
        assert result.entry_count == 1
        assert result.truncated is False
        assert result.depth == 8  # default
        assert result.text.startswith("Workspace files")

    def test_custom_config_depth_in_result(self, tmp_path: Path) -> None:
        config = ManifestConfig(max_depth=3)
        ws = _build_workspace(tmp_path, files=["a.py"])
        result = WorkspaceManifest(config=config).build(ws)
        assert result.depth == 3
