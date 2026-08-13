"""Tests for per-tier context routing (TD-508).

Covers, per spec §4.6:

- Brain receives full steering + memory + workspace manifest
- Worker receives steering + current task + relevant files, no manifest/memory
- Validator receives only the standards/conventions steering subset plus the
  diff and test output — nothing else
- Subset selection is configurable, with a documented default
- Each tier's assembled prompt contains and excludes the expected blocks
"""

from __future__ import annotations

from pathlib import Path

from tstd.context.tier import (
    DEFAULT_VALIDATOR_SUBSET,
    TierContext,
    TierContextConfig,
    assemble_for_tier,
    assemble_for_tier_sync,
    default_config_for_tier,
)


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
    """Build a workspace in *base*; return ``(home_dir, workspace)``.

    The home dir is a ``.tstdesk`` sibling of the workspace so the
    resolver can find the user-global file (test_context.py convention).
    """
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


# ── Defaults ─────────────────────────────────────────────────────────────


class TestDefaults:
    def test_default_validator_subset_documented(self) -> None:
        """The documented default subset covers the conventions doc and
        standards/conventions/style rules, matched by basename."""
        assert "AGENTS.md" in DEFAULT_VALIDATOR_SUBSET
        assert any("standards" in p for p in DEFAULT_VALIDATOR_SUBSET)

    def test_default_config_per_tier(self) -> None:
        """Every tier gets a default config with the documented subset."""
        for tier in ("brain", "worker", "validator"):
            cfg = default_config_for_tier(tier)
            assert isinstance(cfg, TierContextConfig)
            assert cfg.validator_subset == DEFAULT_VALIDATOR_SUBSET


# ── Brain tier ───────────────────────────────────────────────────────────


class TestBrainTier:
    def test_brain_has_full_steering(self, tmp_path: Path) -> None:
        """Brain receives every steering source."""
        home, ws = _build_workspace(
            tmp_path,
            global_file="global: be thorough",
            root_file="root: use python3",
            rules={"standards.md": "rules: lint before commit"},
        )
        result = assemble_for_tier_sync(
            "brain",
            workspace_path=ws,
            home_dir=home,
        )
        assert "global: be thorough" in result.text
        assert "root: use python3" in result.text
        assert "rules: lint before commit" in result.text

    def test_brain_has_manifest_and_memory(self, tmp_path: Path) -> None:
        """Brain's block includes memory and the workspace manifest."""
        home, ws = _build_workspace(tmp_path, root_file="steering")
        result = assemble_for_tier_sync(
            "brain",
            workspace_path=ws,
            home_dir=home,
            memory="remember: we pinned ruff",
            manifest_text="src/main.py\nsrc/utils.py",
        )
        assert result.blocks == {
            "steering": result.blocks["steering"],
            "memory": "remember: we pinned ruff",
            "manifest": "src/main.py\nsrc/utils.py",
        }
        assert "remember: we pinned ruff" in result.text
        assert "src/main.py" in result.text

    def test_brain_excludes_worker_and_validator_blocks(self, tmp_path: Path) -> None:
        """Brain does not get task, relevant files, diff, or test output."""
        home, ws = _build_workspace(tmp_path, root_file="steering")
        result = assemble_for_tier_sync(
            "brain",
            workspace_path=ws,
            home_dir=home,
            task="secret task",
            matched_paths={"src/secret.py"},
            diff="+secret diff",
            test_output="secret output",
        )
        assert "secret task" not in result.text
        assert "src/secret.py" not in result.text
        assert "secret diff" not in result.text
        assert "secret output" not in result.text
        assert set(result.blocks) == {"steering"}


# ── Worker tier ──────────────────────────────────────────────────────────


class TestWorkerTier:
    def test_worker_has_steering_task_and_relevant_files(self, tmp_path: Path) -> None:
        """Worker receives steering, the current task, and relevant files."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="root: use async",
            rules={"workflow.md": "rules: write a test"},
        )
        result = assemble_for_tier_sync(
            "worker",
            workspace_path=ws,
            home_dir=home,
            task="Add timeout handling",
            matched_paths={"src/handler.py"},
        )
        assert "root: use async" in result.text
        assert "rules: write a test" in result.text
        assert "Add timeout handling" in result.text
        assert "src/handler.py" in result.text
        assert set(result.blocks) == {"steering", "task", "relevant_files"}

    def test_worker_excludes_manifest_and_memory(self, tmp_path: Path) -> None:
        """Worker's block never includes manifest or memory, even if supplied."""
        home, ws = _build_workspace(tmp_path, root_file="steering")
        result = assemble_for_tier_sync(
            "worker",
            workspace_path=ws,
            home_dir=home,
            manifest_text="src/main.py",
            memory="remember: secrets stay in keychain",
        )
        assert "src/main.py" not in result.text
        assert "remember: secrets stay in keychain" not in result.text
        assert set(result.blocks) == {"steering"}

    def test_worker_steering_respects_path_scope(self, tmp_path: Path) -> None:
        """Path-scoped rules (TD-503) still filter the worker's steering."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="root: general",
            rules={
                "api.md": '---\nappliesTo: ["src/api/**"]\n---\nrules: api only',
            },
        )
        result = assemble_for_tier_sync(
            "worker",
            workspace_path=ws,
            home_dir=home,
            task="Fix UI",
            matched_paths={"src/ui/button.py"},
        )
        # The api rule matched no touched path → absent from the block.
        assert "rules: api only" not in result.text
        assert "root: general" in result.text


# ── Validator tier ───────────────────────────────────────────────────────


class TestValidatorTier:
    def test_validator_gets_default_subset(self, tmp_path: Path) -> None:
        """With the default subset, the validator sees the conventions doc
        and standards/conventions/style rules — but not other rules."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="# Coding Standards\n\nUse type hints everywhere.",
            rules={
                "standards.md": "Standard: lint before commit.",
                "style.md": "Style: 4-space indent.",
                "workflow.md": "Workflow: branch per story.",
            },
        )
        result = assemble_for_tier_sync(
            "validator",
            workspace_path=ws,
            home_dir=home,
            diff="diff --git a/src/main.py",
            test_output="3 passed, 0 failed",
        )
        assert "Use type hints" in result.text  # workspace conventions doc
        assert "lint before commit" in result.text  # standards*.md
        assert "4-space indent" in result.text  # style*.md
        assert "Workflow: branch per story" not in result.text  # not a standards rule
        assert "diff --git a/src/main.py" in result.text
        assert "3 passed, 0 failed" in result.text
        assert set(result.blocks) == {"steering", "diff", "test_output"}

    def test_validator_excludes_everything_else(self, tmp_path: Path) -> None:
        """Validator gets only its subset + diff + test output."""
        home, ws = _build_workspace(
            tmp_path,
            global_file="global: personal prefs",
            root_file="root: conventions",
        )
        result = assemble_for_tier_sync(
            "validator",
            workspace_path=ws,
            home_dir=home,
            task="secret task",
            matched_paths={"src/secret.py"},
            manifest_text="src/main.py",
            memory="remember: secrets stay in keychain",
            diff="+changed",
            test_output="ok",
        )
        # The default subset includes AGENTS.md (basename match), so both
        # the workspace and user-global AGENTS.md are present.
        assert "root: conventions" in result.text
        assert "global: personal prefs" in result.text
        assert "secret task" not in result.text
        assert "src/secret.py" not in result.text
        assert "src/main.py" not in result.text
        assert "remember: secrets stay in keychain" not in result.text
        assert set(result.blocks) == {"steering", "diff", "test_output"}


# ── Configurable subset ──────────────────────────────────────────────────


class TestConfigurableSubset:
    def test_custom_subset_replaces_default(self, tmp_path: Path) -> None:
        """A custom validator_subset replaces (not extends) the default."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="# Standards\n\nBe thorough.",
            rules={
                "security.md": "Security: no plaintext secrets.",
                "style.md": "Style: 4-space indent.",
                "logging.md": "Logging: structured logs.",
            },
        )
        config = TierContextConfig(validator_subset=("security.md", "style.md"))
        result = assemble_for_tier_sync(
            "validator",
            workspace_path=ws,
            home_dir=home,
            diff="diff",
            test_output="ok",
            config=config,
        )
        assert "no plaintext secrets" in result.text
        assert "4-space indent" in result.text
        # Root conventions doc and the logging rule are no longer selected.
        assert "Be thorough" not in result.text
        assert "structured logs" not in result.text

    def test_empty_subset_drops_steering(self, tmp_path: Path) -> None:
        """An empty subset means the validator sees no steering at all."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="# Standards\n\nBe thorough.",
            rules={"security.md": "Security rule."},
        )
        config = TierContextConfig(validator_subset=())
        result = assemble_for_tier_sync(
            "validator",
            workspace_path=ws,
            home_dir=home,
            diff="diff",
            test_output="ok",
            config=config,
        )
        assert set(result.blocks) == {"diff", "test_output"}
        assert "Be thorough" not in result.text
        assert "Security rule" not in result.text


# ── Async entry point ────────────────────────────────────────────────────


class TestAsyncEntryPoint:
    async def test_assemble_for_tier_async(self, tmp_path: Path) -> None:
        """The async entry point composes the same per-tier context."""
        home, ws = _build_workspace(tmp_path, root_file="root: steering")
        result = await assemble_for_tier(
            "worker",
            workspace_path=ws,
            home_dir=home,
            task="async task",
            matched_paths={"src/a.py"},
        )
        assert isinstance(result, TierContext)
        assert "root: steering" in result.text
        assert "async task" in result.text
        assert "src/a.py" in result.text
