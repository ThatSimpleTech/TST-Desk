"""Surgical catalog and tier-binding writes (TD-1717)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tstd.config import ConfigError, default_config_yaml, load_config
from tstd.config_write import delete_credential_entry, save_credential, save_tier_credential


def _seed(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(default_config_yaml(), encoding="utf-8")
    return path


class TestSaveCredential:
    def test_adds_a_named_row(self, tmp_path: Path) -> None:
        path = _seed(tmp_path)
        save_credential("local", "Local", path)
        cfg = load_config(path)
        assert cfg.credentials["local"].name == "Local"
        # Comments around the local preset survive.
        assert "compaction budget" in path.read_text()

    def test_renames_in_place(self, tmp_path: Path) -> None:
        path = _seed(tmp_path)
        save_credential("openrouter", "OR", path)
        assert load_config(path).credentials["openrouter"].name == "OR"

    def test_rejects_reserved_id(self, tmp_path: Path) -> None:
        path = _seed(tmp_path)
        with pytest.raises(ConfigError, match="reserved"):
            save_credential("ntfy-topic", "Ntfy", path)


class TestDeleteCredential:
    def test_removes_the_row(self, tmp_path: Path) -> None:
        path = _seed(tmp_path)
        save_credential("local", "Local", path)
        delete_credential_entry("local", path)
        assert "local" not in load_config(path).credentials

    def test_unknown_row_is_an_error(self, tmp_path: Path) -> None:
        path = _seed(tmp_path)
        with pytest.raises(ConfigError, match="no credential"):
            delete_credential_entry("nope", path)


class TestSaveTierCredential:
    def test_binds_and_unbinds(self, tmp_path: Path) -> None:
        path = _seed(tmp_path)
        save_credential("local", "Local", path)
        save_tier_credential("local", "brain", "local", path)
        cfg = load_config(path)
        assert cfg.presets["local"].brain.credential == "local"
        save_tier_credential("local", "brain", None, path)
        cfg = load_config(path)
        assert cfg.presets["local"].brain.credential is None

    def test_unknown_credential_is_an_error(self, tmp_path: Path) -> None:
        path = _seed(tmp_path)
        with pytest.raises(ConfigError, match="Unknown credential"):
            save_tier_credential("local", "brain", "nope", path)

    def test_secret_never_lands_in_yaml(self, tmp_path: Path) -> None:
        path = _seed(tmp_path)
        save_credential("local", "Local", path)
        dumped = yaml.safe_load(path.read_text())
        assert "sk-" not in path.read_text()
        assert dumped["credentials"]["local"] == {"name": "Local"}
