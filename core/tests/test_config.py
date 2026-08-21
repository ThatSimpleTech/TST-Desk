"""Tests for model configuration schema (TD-302).

Covers YAML loading, validation, presets, actionable error messages,
and the "no slugs in source code" assertion.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from tstd.config import (
    DEFAULT_LOG_MAX_EVENTS,
    PRESETS,
    ComputerUseConfig,
    ConfigError,
    EmbeddingsConfig,
    ModelConfig,
    SlackNotifyConfig,
    TierConfig,
    cached_config,
    default_config_yaml,
    ensure_user_config,
    load_config,
)

# ── Helpers ──────────────────────────────────────────────────────────────


def _write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(content)
    return p


def _load_shipped(tmp_path: Path) -> ModelConfig:
    """Load the shipped default config from a fixture path.

    Never ``load_config()`` with no path: that resolves to
    ``user_data_dir()/config.yaml`` — the developer's own file, whose
    preset and pinned slugs are none of this suite's business (TD-1408).
    The fixture IS the shipped default, so these tests still fail the day
    the shipped config drifts from the values pinned below.
    """
    return load_config(_write_config(tmp_path, default_config_yaml()))


DEFAULT_YAML_SNIPPET = default_config_yaml()[:200]


# ── Loading ──────────────────────────────────────────────────────────────


class TestLoading:
    def test_default_config_is_valid(self, tmp_path: Path) -> None:
        """The shipped config.yaml must pass validation."""
        cfg = _load_shipped(tmp_path)
        assert isinstance(cfg, ModelConfig)
        assert cfg.active_preset == "tst-default"
        assert cfg.session.log_max_events == DEFAULT_LOG_MAX_EVENTS

    def test_session_log_max_events_zero_is_rejected(self, tmp_path: Path) -> None:
        text = default_config_yaml().replace("log_max_events: 10000", "log_max_events: 0")
        path = _write_config(tmp_path, text)
        with pytest.raises(ConfigError, match="log_max_events"):
            load_config(path)

    def test_all_presets_are_present(self, tmp_path: Path) -> None:
        """Shipped config has all 3 presets."""
        cfg = _load_shipped(tmp_path)
        assert sorted(cfg.presets) == sorted(PRESETS)

    def test_each_preset_has_all_tiers(self, tmp_path: Path) -> None:
        """Every preset has brain, worker, and validator.

        Tier presence, not slug presence: a loopback tier may leave its slug
        to discovery (TD-1805), so asserting a slug here would assert the
        opposite of what the local preset ships.
        """
        cfg = _load_shipped(tmp_path)
        for name, preset in cfg.presets.items():
            assert preset.brain.base_url, f"{name} missing brain"
            assert preset.worker.base_url, f"{name} missing worker"
            assert preset.validator.base_url, f"{name} missing validator"

    def test_default_config_yaml_returns_string(self) -> None:
        content = default_config_yaml()
        assert "moonshotai/kimi-k3" in content
        assert "deepseek/deepseek-v4-flash" in content

    def test_ensure_user_config_creates_if_missing(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        assert not config_path.exists()
        result = ensure_user_config(config_path)
        assert result == config_path
        assert config_path.exists()
        assert "moonshotai/kimi-k3" in config_path.read_text()

    def test_ensure_user_config_does_not_overwrite(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text("# custom")
        ensure_user_config(config_path)
        assert config_path.read_text() == "# custom"

    def test_cached_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """cached_config returns the same object on repeated calls.

        The no-path loader resolves through user_data_dir(); redirect it so
        the identity check never reads the developer's own file (TD-1408).
        """
        monkeypatch.setattr("tstd.config.user_data_dir", lambda: tmp_path)
        cached_config.cache_clear()
        try:
            c1 = cached_config()
            c2 = cached_config()
        finally:
            cached_config.cache_clear()
        assert c1 is c2


# ── Tiers and presets ────────────────────────────────────────────────────


class TestTiers:
    def test_tier_method(self, tmp_path: Path) -> None:
        cfg = _load_shipped(tmp_path)
        brain = cfg.tier("brain")
        assert isinstance(brain, TierConfig)
        assert brain.slug == "moonshotai/kimi-k3"

    def test_tiers_method(self, tmp_path: Path) -> None:
        cfg = _load_shipped(tmp_path)
        tiers = cfg.tiers()
        assert sorted(tiers) == ["brain", "validator", "worker"]
        assert tiers["brain"].slug == "moonshotai/kimi-k3"
        assert tiers["worker"].slug == "deepseek/deepseek-v4-flash"
        assert tiers["validator"].slug == "deepseek/deepseek-v4-pro"

    def test_tier_alternate_preset(self, tmp_path: Path) -> None:
        cfg = _load_shipped(tmp_path)
        cfg.active_preset = "budget"
        assert cfg.tier("brain").slug == "z-ai/glm-5.2"

    def test_tier_local_preset(self, tmp_path: Path) -> None:
        cfg = _load_shipped(tmp_path)
        cfg.active_preset = "local"
        assert cfg.tier("brain").input_price == 0.0
        assert cfg.tier("worker").input_price == 0.0

    def test_worker_max_output(self, tmp_path: Path) -> None:
        """Worker tier defaults to 16K max_output_tokens for edits."""
        cfg = _load_shipped(tmp_path)
        assert cfg.tier("worker").max_output_tokens == 16384

    def test_shipped_embeddings_command_is_empty(self, tmp_path: Path) -> None:
        """TD-2204: packaged config is attach-only (no host spawn)."""
        cfg = _load_shipped(tmp_path)
        assert cfg.embeddings.command == ""
        assert EmbeddingsConfig().command == ""

    def test_shipped_computer_use_command_is_empty(self, tmp_path: Path) -> None:
        """TD-3301: packaged config is mock-only (no sidecar spawn)."""
        cfg = _load_shipped(tmp_path)
        assert cfg.computer_use.command == ""
        assert ComputerUseConfig().command == ""

    def test_shipped_slack_notify_is_off(self, tmp_path: Path) -> None:
        """TD-3801: packaged config does not send to Slack."""
        cfg = _load_shipped(tmp_path)
        assert cfg.notify.slack.enabled is False
        assert cfg.notify.slack.host == ""
        assert SlackNotifyConfig().enabled is False

    def test_computer_use_command_accepts_string_or_list(self) -> None:
        assert ComputerUseConfig(command="python -m tst_cu_mcp").command == ("python -m tst_cu_mcp")
        assert ComputerUseConfig.model_validate(
            {"command": ["python", "-m", "tst_cu_mcp"]}
        ).command == ["python", "-m", "tst_cu_mcp"]

    def test_embeddings_command_accepts_string_or_list(self) -> None:
        assert EmbeddingsConfig(command="llama-server --embeddings").command == (
            "llama-server --embeddings"
        )
        assert EmbeddingsConfig.model_validate(
            {"command": ["llama-server", "--port", 8080]}
        ).command == ["llama-server", "--port", "8080"]


# ── Validation ───────────────────────────────────────────────────────────


class TestValidation:
    def test_missing_presets_key(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, "active_preset: tst-default\n")
        with pytest.raises(ConfigError, match="presets"):
            load_config(path)

    def test_missing_tier_in_preset(self, tmp_path: Path) -> None:
        path = _write_config(
            tmp_path,
            """
presets:
  tst-default:
    brain:
      slug: test
      base_url: http://localhost
      input_price: 0
      output_price: 0
      cache_read_price: 0
      context_window: 100
      max_output_tokens: 100
""",
        )
        with pytest.raises(ConfigError, match="worker"):
            load_config(path)

    def test_negative_price(self, tmp_path: Path) -> None:
        path = _write_config(
            tmp_path,
            """
presets:
  tst-default:
    brain:
      slug: test
      base_url: http://localhost
      input_price: -1
      output_price: 0
      cache_read_price: 0
      context_window: 100
      max_output_tokens: 100
    worker:
      slug: test
      base_url: http://localhost
      input_price: 0
      output_price: 0
      cache_read_price: 0
      context_window: 100
      max_output_tokens: 100
    validator:
      slug: test
      base_url: http://localhost
      input_price: 0
      output_price: 0
      cache_read_price: 0
      context_window: 100
      max_output_tokens: 100
""",
        )
        with pytest.raises(ConfigError) as excinfo:
            load_config(path)
        assert "input_price" in str(excinfo.value)

    def test_empty_slug(self, tmp_path: Path) -> None:
        path = _write_config(
            tmp_path,
            """
presets:
  tst-default:
    brain:
      slug: ""
      base_url: http://localhost
      input_price: 0
      output_price: 0
      cache_read_price: 0
      context_window: 100
      max_output_tokens: 100
    worker:
      slug: test
      base_url: http://localhost
      input_price: 0
      output_price: 0
      cache_read_price: 0
      context_window: 100
      max_output_tokens: 100
    validator:
      slug: test
      base_url: http://localhost
      input_price: 0
      output_price: 0
      cache_read_price: 0
      context_window: 100
      max_output_tokens: 100
""",
        )
        with pytest.raises(ConfigError) as excinfo:
            load_config(path)
        assert "slug" in str(excinfo.value)
        assert "String should have at least 1 character" in str(excinfo.value)

    def test_active_preset_missing(self, tmp_path: Path) -> None:
        path = _write_config(
            tmp_path,
            """
active_preset: nonexistent
presets:
  tst-default:
    brain:
      slug: a
      base_url: http://localhost
      input_price: 0
      output_price: 0
      cache_read_price: 0
      context_window: 100
      max_output_tokens: 100
    worker:
      slug: b
      base_url: http://x
      input_price: 0
      output_price: 0
      cache_read_price: 0
      context_window: 100
      max_output_tokens: 100
    validator:
      slug: c
      base_url: http://localhost
      input_price: 0
      output_price: 0
      cache_read_price: 0
      context_window: 100
      max_output_tokens: 100
""",
        )
        with pytest.raises(ConfigError, match="nonexistent"):
            load_config(path)

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, "{invalid: yaml: broken\n")
        with pytest.raises(ConfigError, match="Invalid YAML"):
            load_config(path)

    def test_not_a_mapping(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, "just a string\n")
        with pytest.raises(ConfigError, match="mapping"):
            load_config(path)


# ─ Slug detection in source code ───────────────────────────────────────────


class TestNoSlugsInSource:
    """Assert no model slug or price appears in Python source code (TD-302).

    Greps only ``core/tstd/`` (the shipped package), not test files or
    config.yaml (which legitimately contains the slugs).
    """

    # Known slugs that MUST NOT appear in .py files.
    # They belong in config.yaml only.
    _FORBIDDEN_PATTERNS: ClassVar[list[str]] = [
        "moonshotai/kimi-k3",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
    ]

    # Prices that MUST NOT appear in source code.
    # These are matched as whole words that look like dollar figures,
    # not as substrings (to avoid false matches like 0o600 matching 0.60).
    _FORBIDDEN_PRICES: ClassVar[list[str]] = [
        "2.80",
        "14.00",
        "0.30",
        "0.07",
        "0.17",
        "0.44",
        "0.87",
        "0.15",
        "0.60",
    ]

    @staticmethod
    def _source_dir() -> str:
        """Return the path to the tstd source package from the test directory."""
        import os

        # Tests run from core/tests/; source is in core/tstd/
        return os.path.join(os.path.dirname(__file__), "..", "tstd")

    @staticmethod
    def _grep_for(pattern: str, src_dir: str) -> str | None:
        """Grep for ``pattern`` in ``src_dir``, returning matched lines or None."""
        import subprocess

        result = subprocess.run(
            ["grep", "-rnF", "--include=*.py", pattern, src_dir],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None

    def test_no_slugs_in_python_files(self) -> None:
        """Grepping for known slugs should only hit config.yaml."""
        src = self._source_dir()
        for pattern in self._FORBIDDEN_PATTERNS:
            found = self._grep_for(pattern, src)
            assert found is None, f"Slug '{pattern}' found in Python source (.py files):\n{found}"

    def test_no_prices_in_python_files(self) -> None:
        """Prices must live in config.yaml, not in source code."""
        src = self._source_dir()
        for price in self._FORBIDDEN_PRICES:
            found = self._grep_for(price, src)
            assert found is None, f"Price '{price}' found in Python source (.py files):\n{found}"
