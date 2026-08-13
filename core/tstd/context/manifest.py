"""Workspace manifest — file tree for the model's awareness (TD-507).

The manifest is built from the workspace, respecting ``.gitignore`` and
configurable ignore patterns.  It is depth-capped, entry-capped, and
size-capped so large repositories don't stall session start.

Primary method: ``git ls-files`` (tracks + untracked, respects gitignore
natively).  Fallback: ``os.walk`` with ignore patterns for non-git repos.

The result is cached with a debounce so it is not rebuilt on every turn
(criterion 3).  The daemon invalidates the cache when the workspace
changes (TD-305).
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# Entries deeper than this are excluded from the manifest.
_DEFAULT_MAX_DEPTH = 8

# Hard cap on the number of files in the manifest.
_DEFAULT_MAX_ENTRIES = 20_000

# Total rendered text size cap (5 MB).  We do not send megabytes of
# filenames into the prompt.
_DEFAULT_MAX_BYTES = 5_242_880

# Directories always excluded from the walk fallback (git already handles
# these via .gitignore, but the fallback path needs its own list).
_FALLBACK_IGNORE = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        ".tst",
    }
)

# How long (in seconds) a cached manifest is considered fresh.
_DEBOUNCE_SECONDS = 60


@dataclass(frozen=True)
class ManifestConfig:
    """Configuration for the workspace manifest.

    Attributes:
        max_depth: Maximum directory depth (relative to the workspace
            root).  Files deeper than this are excluded.
        max_entries: Maximum number of files in the manifest.  Truncated
            when exceeded, with a comment marking the cut.
        max_bytes: Maximum rendered text size.  Truncated when exceeded.
        ignore_patterns: Additional directory name patterns to exclude
            (used only by the ``os.walk`` fallback).  These supplement
            the default set.
    """

    max_depth: int = _DEFAULT_MAX_DEPTH
    max_entries: int = _DEFAULT_MAX_ENTRIES
    max_bytes: int = _DEFAULT_MAX_BYTES
    ignore_patterns: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class ManifestResult:
    """The generated manifest.

    Attributes:
        text: The rendered manifest text (one file per line, with a
            header and optional truncation markers).
        entry_count: Number of files in the manifest (before truncation
            for the byte cap).
        truncated: ``True`` when the entry cap or byte cap was hit.
        depth: The effective depth limit applied.
    """

    text: str
    entry_count: int
    truncated: bool
    depth: int


class WorkspaceManifest:
    """Generates and caches the workspace manifest."""

    def __init__(self, config: ManifestConfig | None = None) -> None:
        self._config = config or ManifestConfig()
        self._cache: dict[str, tuple[float, ManifestResult]] = {}

    def build(self, workspace_path: str | Path) -> ManifestResult:
        """Build or return a cached manifest for *workspace_path*.

        The result is cached for ``_DEBOUNCE_SECONDS`` (60) so it is
        not rebuilt on every turn (criterion 3).  Callers in the daemon
        invalidate the cache explicitly when the workspace changes.
        """
        cache_key = str(Path(workspace_path).resolve())
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached is not None and now - cached[0] < _DEBOUNCE_SECONDS:
            return cached[1]

        result = self._build(workspace_path)
        self._cache[cache_key] = (now, result)
        return result

    def invalidate(self, workspace_path: str | Path) -> None:
        """Force the next call to rebuild the manifest."""
        cache_key = str(Path(workspace_path).resolve())
        self._cache.pop(cache_key, None)

    def _build(self, workspace_path: str | Path) -> ManifestResult:
        workspace = Path(workspace_path).resolve()
        config = self._config

        if self._is_git_repo(workspace):
            return self._build_from_git(workspace, config)
        return self._build_from_walk(workspace, config)

    @staticmethod
    def _is_git_repo(workspace: Path) -> bool:
        return (workspace / ".git").exists()

    def _build_from_git(self, workspace: Path, config: ManifestConfig) -> ManifestResult:
        """Build manifest using ``git ls-files``.

        This respects ``.gitignore`` natively and handles tracked +
        untracked files correctly.
        """
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "ls-files",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            # Git unavailable or timeout → fall back to walk
            return self._build_from_walk(workspace, config)

        if result.returncode != 0:
            return self._build_from_walk(workspace, config)

        return self._render_lines(
            result.stdout.splitlines(),
            config,
        )

    def _build_from_walk(self, workspace: Path, config: ManifestConfig) -> ManifestResult:
        """Build manifest via ``os.walk`` (non-git repos)."""
        ignore = _FALLBACK_IGNORE | config.ignore_patterns
        entries: list[str] = []

        for root, dirs, files in os.walk(workspace, followlinks=False):
            # Prune ignored dirs
            dirs[:] = sorted(d for d in dirs if d not in ignore)
            root_path = Path(root)
            rel = root_path.relative_to(workspace)
            depth = len(rel.parts) if rel.parts else 0
            if depth + 1 > config.max_depth:
                dirs.clear()  # don't descend further
                continue

            for file in sorted(files):
                rel_path = str(rel / file) if rel.parts else file
                entries.append(rel_path)

                # Collect one past the cap so the renderer can detect
                # truncation (the renderer trims to max_entries).
                if len(entries) > config.max_entries:
                    break
            if len(entries) > config.max_entries:
                break

        return self._render_lines(entries, config)

    @staticmethod
    def _render_lines(
        lines: list[str],
        config: ManifestConfig,
    ) -> ManifestResult:
        """Render the file list with header and optional truncation.

        Applies depth and byte caps, then adds a truncation marker
        if either was exceeded.
        """
        # Apply depth cap: components = depth + 1, exclude if > max_depth
        filtered: list[str] = []
        for line in lines:
            components = line.count("/") + 1
            if components > config.max_depth:
                continue
            filtered.append(line)

        # Apply entry cap
        truncated = False
        if len(filtered) > config.max_entries:
            filtered = filtered[: config.max_entries]
            truncated = True

        # Build text
        header = (
            f"Workspace files (up to {config.max_depth} levels deep, {config.max_entries} max):"
        )
        parts = [header]
        parts.extend(filtered)

        if truncated:
            parts.append(
                f"<!-- ... truncated at {config.max_entries} entries "
                f"(limit: {config.max_entries}) ... -->"
            )

        text = "\n".join(parts)

        # Apply byte cap
        if len(text.encode("utf-8")) > config.max_bytes:
            # Truncate at the byte boundary
            encoded = text.encode("utf-8")[: config.max_bytes]
            text = encoded.decode("utf-8", errors="replace")
            text += (
                f"\n<!-- ... truncated at {config.max_bytes} bytes "
                f"(limit: {config.max_bytes}) ... -->"
            )
            truncated = True

        return ManifestResult(
            text=text,
            entry_count=len(filtered),
            truncated=truncated,
            depth=config.max_depth,
        )
