"""Tests for cache-aware prompt assembly (TD-305).

Covers:
- Stable-prefix order: base → steering → memory → manifest
- Prefix byte-identity across calls with unchanged source files
- Prefix change when steering sources change
- Worker: no manifest, no memory placeholder
- Validator: subset only + diff + test output
- Memory placeholder for brain (spec §5 placeholder)
- Async entry point and prefix token count
"""

from __future__ import annotations

from pathlib import Path

from tstd.context import (
    BASE_SYSTEM_PROMPT,
    MEMORY_PLACEHOLDER,
    PromptAssembler,
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
