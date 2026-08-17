"""Tests for cache-aware prompt assembly (TD-305, TD-1810).

Covers:
- Stable-prefix order: base → workspace root → steering → memory → manifest
- Prefix byte-identity across calls with unchanged source files
- Prefix change when steering sources change
- Worker: no manifest, no memory placeholder
- Validator: subset only + diff + test output
- Memory placeholder for brain (spec §5 placeholder)
- Async entry point and prefix token count
- The absolute workspace root, stated once, inside the cache prefix, with
  the manifest's relative listing left intact (TD-1810)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tstd.context import (
    BASE_SYSTEM_PROMPT,
    MEMORY_PLACEHOLDER,
    WORKSPACE_ROOT_LABEL,
    PromptAssembler,
    WorkspaceManifest,
)
from tstd.context.prompt import AssembledPrompt

# ── Fixture helpers ──────────────────────────────────────────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _build_workspace(
    base: Path,
    *,
    global_file: str | None = None,
    root_file: str | None = None,
    rules: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Build a workspace; return ``(home_dir, workspace)``."""
    home = base / "home"
    ws = base / "workspace"
    if global_file is not None:
        _write(home / ".tstdesk" / "AGENTS.md", global_file)
    if root_file is not None:
        _write(ws / "AGENTS.md", root_file)
    if rules:
        for name, content in rules.items():
            _write(ws / ".tst" / "rules" / name, content)
    return home, ws


# ── Stable-prefix order ──────────────────────────────────────────────────


class TestStablePrefixOrder:
    def test_brain_order(self, tmp_path: Path) -> None:
        """Brain prompt: base → steering → memory placeholder → manifest."""
        home, ws = _build_workspace(tmp_path, root_file="root: use python3")
        _write(ws / "src" / "main.py", "print('hi')")
        assembler = PromptAssembler(ws, home_dir=home)
        result = assembler.assemble_sync("brain")
        assert result.text.startswith(BASE_SYSTEM_PROMPT)
        # Base comes before steering
        base_idx = result.text.index(BASE_SYSTEM_PROMPT)
        steering_idx = result.text.index("root: use python3")
        assert base_idx < steering_idx
        # Memory placeholder comes after steering
        memory_idx = result.text.index(MEMORY_PLACEHOLDER)
        assert steering_idx < memory_idx
        # Manifest comes after memory
        manifest_idx = result.text.index("src/main.py")
        assert memory_idx < manifest_idx

    def test_worker_order(self, tmp_path: Path) -> None:
        """Worker prompt: base → steering → task."""
        home, ws = _build_workspace(tmp_path, root_file="root: be async")
        assembler = PromptAssembler(ws, home_dir=home)
        result = assembler.assemble_sync("worker", task="Fix the bug")
        base_idx = result.text.index(BASE_SYSTEM_PROMPT)
        steering_idx = result.text.index("root: be async")
        task_idx = result.text.index("Fix the bug")
        assert base_idx < steering_idx < task_idx
        # No manifest, no memory placeholder
        assert MEMORY_PLACEHOLDER not in result.text
        assert "src/main.py" not in result.text

    def test_validator_order(self, tmp_path: Path) -> None:
        """Validator prompt: base → steering subset → diff → test output."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="# Standards",
            rules={"standards.md": "Standard: lint."},
        )
        assembler = PromptAssembler(ws, home_dir=home)
        result = assembler.assemble_sync(
            "validator",
            diff="+new code",
            test_output="3 passed",
        )
        base_idx = result.text.index(BASE_SYSTEM_PROMPT)
        steering_idx = result.text.index("Standard: lint")
        diff_idx = result.text.index("+new code")
        test_idx = result.text.index("3 passed")
        assert base_idx < steering_idx < diff_idx < test_idx
        # No manifest, no memory, no task
        assert MEMORY_PLACEHOLDER not in result.text
        assert "Fix the bug" not in result.text

    def test_memory_placeholder_included(self, tmp_path: Path) -> None:
        """Brain with no memory includes the placeholder."""
        home, ws = _build_workspace(tmp_path, root_file="root: steering")
        assembler = PromptAssembler(ws, home_dir=home)
        result = assembler.assemble_sync("brain")
        assert MEMORY_PLACEHOLDER in result.text


# ── Prefix stability ────────────────────────────────────────────────────


class TestPrefixStability:
    def test_prefix_identical_across_calls(self, tmp_path: Path) -> None:
        """Blocks 1-2 are byte-identical across calls with unchanged files."""
        home, ws = _build_workspace(tmp_path, root_file="root: conventions")
        assembler = PromptAssembler(ws, home_dir=home)
        a1 = assembler.assemble_sync("brain")
        a2 = assembler.assemble_sync("brain")
        assert a1.prefix_hash == a2.prefix_hash
        assert a1.prefix == a2.prefix

    def test_prefix_changes_when_steering_changes(self, tmp_path: Path) -> None:
        """Prefix hash changes when a steering file is modified."""
        home, ws = _build_workspace(tmp_path, root_file="root: original")
        assembler = PromptAssembler(ws, home_dir=home)
        original = assembler.assemble_sync("brain")

        # Modify the root steering file
        _write(ws / "AGENTS.md", "root: modified")
        changed = assembler.assemble_sync("brain")

        assert original.prefix_hash != changed.prefix_hash

    def test_prefix_stable_across_tiers(self, tmp_path: Path) -> None:
        """Blocks 1-2 are the same for brain and worker (same steering)."""
        home, ws = _build_workspace(tmp_path, root_file="root: steering")
        assembler = PromptAssembler(ws, home_dir=home)
        brain = assembler.assemble_sync("brain")
        worker = assembler.assemble_sync("worker")
        # Same steering → same prefix
        assert brain.prefix_hash == worker.prefix_hash
        # But the full text differs (worker has no manifest)
        assert brain.text != worker.text

    def test_prefix_tokens_positive(self, tmp_path: Path) -> None:
        """Prefix token count is greater than zero."""
        home, ws = _build_workspace(tmp_path, root_file="root: steering")
        assembler = PromptAssembler(ws, home_dir=home)
        result = assembler.assemble_sync("brain")
        assert result.prefix_tokens > 0


# ── Async entry point ────────────────────────────────────────────────────


class TestAsyncEntry:
    async def test_async_assemble(self, tmp_path: Path) -> None:
        """Async entry point works and returns AssembledPrompt."""
        home, ws = _build_workspace(tmp_path, root_file="root: steering")
        assembler = PromptAssembler(ws, home_dir=home)
        result = await assembler.assemble("brain")
        assert isinstance(result, AssembledPrompt)
        assert result.prefix_hash
        assert result.text
        assert "root: steering" in result.text


# ── Workspace root (TD-1810) ─────────────────────────────────────────────


def _root_from_prompt(text: str) -> str:
    """Recover the workspace root using only the prompt, as a reader would.

    Deliberately naive — one labelled line, read off the front — because
    that is the whole claim being tested: a reader given nothing but the
    prompt can find the root without inference.
    """
    for line in text.splitlines():
        if line.startswith(WORKSPACE_ROOT_LABEL):
            return line[len(WORKSPACE_ROOT_LABEL) :].strip()
    raise AssertionError(f"no {WORKSPACE_ROOT_LABEL!r} line in the prompt")


def _manifest_entries_from_prompt(text: str) -> list[str]:
    """Recover the manifest's listed paths using only the prompt."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("Workspace files ("):
            return [entry for entry in lines[i + 1 :] if entry and not entry.startswith("<!--")]
    raise AssertionError("no workspace manifest header in the prompt")


def _sample_workspace(base: Path) -> tuple[Path, Path]:
    """A workspace with steering and a couple of nested files."""
    home, ws = _build_workspace(base, root_file="root: use python3")
    _write(ws / "README.md", "# readme")
    _write(ws / "src" / "app.py", "print('hi')")
    _write(ws / "src" / "deep" / "nested.py", "x = 1")
    return home, ws


class TestWorkspaceRoot:
    def test_root_stated_though_nothing_asked_for_it(self, tmp_path: Path) -> None:
        """The root is in the prompt with no task and no caller hint.

        Nothing in this call names the workspace path: the assembler is
        given a tier and nothing else, which is the state the loop is in
        when the user's message never mentions where they are.
        """
        home, ws = _sample_workspace(tmp_path)
        result = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        assert _root_from_prompt(result.text) == ws.resolve().as_posix()

    def test_root_stated_once(self, tmp_path: Path) -> None:
        """One labelled statement of the root, not one per manifest entry."""
        home, ws = _sample_workspace(tmp_path)
        result = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        assert result.text.count(WORKSPACE_ROOT_LABEL) == 1

    @pytest.mark.parametrize("tier", ["brain", "worker", "validator"])
    def test_every_tier_is_told_the_root(self, tmp_path: Path, tier: str) -> None:
        """The worker calls the same fs_* tools, so it needs the root too."""
        home, ws = _sample_workspace(tmp_path)
        result = PromptAssembler(ws, home_dir=home).assemble_sync(
            tier,  # type: ignore[arg-type]
            task="Fix the bug",
            diff="+new code",
        )
        assert _root_from_prompt(result.text) == ws.resolve().as_posix()
        assert result.text.count(WORKSPACE_ROOT_LABEL) == 1

    def test_absolute_path_derivable_for_every_manifest_entry(self, tmp_path: Path) -> None:
        """Prompt-only join: every listed file resolves to a real file.

        This is the acceptance criterion in executable form — root and
        entries are both read back out of the assembled text, joined the
        way the prompt says to join them, and checked against the disk.
        No guessing step is available to the reader.
        """
        home, ws = _sample_workspace(tmp_path)
        result = PromptAssembler(ws, home_dir=home).assemble_sync("brain")

        root = _root_from_prompt(result.text)
        entries = _manifest_entries_from_prompt(result.text)
        assert "src/app.py" in entries, entries

        for entry in entries:
            derived = Path(f"{root}/{entry}")
            assert derived.is_absolute(), derived
            assert derived.is_file(), f"{derived} does not exist"

    def test_manifest_listing_is_unchanged(self, tmp_path: Path) -> None:
        """The manifest block is embedded verbatim — entries stay relative."""
        home, ws = _sample_workspace(tmp_path)
        result = PromptAssembler(ws, home_dir=home).assemble_sync("brain")

        manifest = WorkspaceManifest().build(ws).text
        assert manifest in result.text
        assert "src/app.py" in _manifest_entries_from_prompt(result.text)
        # No entry was rewritten to an absolute path.
        for entry in _manifest_entries_from_prompt(result.text):
            assert not entry.startswith("/"), entry

    def test_root_is_inside_the_cache_prefix(self, tmp_path: Path) -> None:
        """The root is session-constant, so it belongs in blocks 1-2."""
        home, ws = _sample_workspace(tmp_path)
        result = PromptAssembler(ws, home_dir=home).assemble_sync("brain")
        assert WORKSPACE_ROOT_LABEL in result.prefix
        assert ws.resolve().as_posix() in result.prefix

    def test_prefix_still_byte_identical_across_calls(self, tmp_path: Path) -> None:
        """Adding the root does not cost the TD-305 stability guarantee."""
        home, ws = _sample_workspace(tmp_path)
        assembler = PromptAssembler(ws, home_dir=home)
        first = assembler.assemble_sync("brain")
        second = assembler.assemble_sync("brain")
        assert first.prefix == second.prefix
        assert first.prefix_hash == second.prefix_hash

    def test_root_precedes_steering_and_the_volatile_blocks(self, tmp_path: Path) -> None:
        """Ordering: base → root → steering → memory → manifest."""
        home, ws = _sample_workspace(tmp_path)
        text = PromptAssembler(ws, home_dir=home).assemble_sync("brain").text
        base_idx = text.index(BASE_SYSTEM_PROMPT)
        root_idx = text.index(WORKSPACE_ROOT_LABEL)
        steering_idx = text.index("root: use python3")
        memory_idx = text.index(MEMORY_PLACEHOLDER)
        manifest_idx = text.index("Workspace files (")
        assert base_idx < root_idx < steering_idx < memory_idx < manifest_idx

    def test_stated_root_is_canonical(self, tmp_path: Path) -> None:
        """A symlinked workspace is stated as the path the guard accepts.

        The path guard canonicalises before comparing, so stating the
        symlink would hand the model a prefix that only accidentally
        matches the boundary it is checked against.
        """
        home, ws = _sample_workspace(tmp_path)
        link = tmp_path / "via-link"
        try:
            os.symlink(ws, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not available on this platform")

        result = PromptAssembler(link, home_dir=home).assemble_sync("brain")
        assert _root_from_prompt(result.text) == ws.resolve().as_posix()
