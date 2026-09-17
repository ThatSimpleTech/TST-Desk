"""Surgical writes of the ``judgments:`` block (TD-708 dev build)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tstd.config import ConfigError, JudgmentsConfig, default_config_yaml, load_config
from tstd.config_write import save_judgments


def _seed(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(default_config_yaml(), encoding="utf-8")
    return path


def _seed_without_judgments(tmp_path: Path) -> Path:
    """A config with the shipped ``judgments:`` section cut, for append tests."""
    path = _seed(tmp_path)
    text = path.read_text(encoding="utf-8")
    head = text.split("\njudgments:", 1)[0]
    path.write_text(head.rstrip("\n") + "\n", encoding="utf-8")
    return path


class TestSaveJudgments:
    def test_appends_the_block_when_absent(self, tmp_path: Path) -> None:
        path = _seed_without_judgments(tmp_path)
        save_judgments(JudgmentsConfig(verification=True, semantic_breaker=True), path)
        cfg = load_config(path)
        assert cfg.judgments.verification is True
        assert cfg.judgments.semantic_breaker is True
        assert cfg.judgments.candidate_selection is False
        assert cfg.judgments.confidence_threshold == 0.6
        assert cfg.judgments.max_state_chars == 2000

    def test_replaces_in_place_without_duplicating(self, tmp_path: Path) -> None:
        path = _seed(tmp_path)
        save_judgments(JudgmentsConfig(verification=True), path)
        save_judgments(JudgmentsConfig(candidate_selection=True, confidence_threshold=0.8), path)
        text = path.read_text(encoding="utf-8")
        assert text.count("judgments:") == 1
        cfg = load_config(path)
        assert cfg.judgments.verification is False
        assert cfg.judgments.candidate_selection is True
        assert cfg.judgments.confidence_threshold == 0.8

    def test_teaching_comments_survive(self, tmp_path: Path) -> None:
        path = _seed(tmp_path)
        before = path.read_text(encoding="utf-8")
        save_judgments(JudgmentsConfig(verification=True, backend="typesafe"), path)
        after = path.read_text(encoding="utf-8")
        # The shipped config's teaching comments are untouched; the block
        # is replaced in place, so everything before it is byte-identical.
        assert "compaction budget" in after
        assert after.split("\njudgments:")[0] == before.split("\njudgments:")[0]
        assert 'backend: typesafe' in after

    def test_rejects_non_judgments_config(self, tmp_path: Path) -> None:
        path = _seed(tmp_path)
        with pytest.raises(ConfigError, match="JudgmentsConfig"):
            save_judgments(object(), path)  # type: ignore[arg-type]
