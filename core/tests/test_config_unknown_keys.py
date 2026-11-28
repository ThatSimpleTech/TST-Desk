"""A config.yaml key this version does not know is ignored and named (UPGRADE-02).

34 config classes refused unknown keys, so one setting a newer build wrote
stopped an older daemon from starting. Now the key is ignored and named,
known keys keep their full validation, and a write keeps the key as it is.
Never touches the live config: every test works in tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tstd.config import ConfigError, default_config_yaml, load_config
from tstd.config_write import save_engine_kind

NEWER = """
autonomy:
  verify_budget_usd: 2.5
workflow:
  review: strict
"""


def _config(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "config.yaml"
    text = default_config_yaml()
    if "\nautonomy:" in text:
        text = text.replace("\nautonomy:\n", "\nautonomy:\n  verify_budget_usd: 2.5\n", 1)
        extra = extra.replace("autonomy:\n  verify_budget_usd: 2.5\n", "")
    path.write_text(text + extra, encoding="utf-8")
    return path


def test_a_newer_builds_keys_load_and_are_named(tmp_path: Path) -> None:
    config = load_config(_config(tmp_path, NEWER))
    assert "autonomy.verify_budget_usd" in config.unknown_keys


def test_a_known_key_keeps_its_validation(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    # A later top-level block replaces the shipped one, so this is the value read.
    path.write_text(
        default_config_yaml() + "\nsession:\n  log_max_events: lots\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="log_max_events"):
        load_config(path)


def test_a_good_file_names_nothing(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(default_config_yaml(), encoding="utf-8")
    assert load_config(path).unknown_keys == []


def test_a_write_keeps_the_unknown_key_verbatim(tmp_path: Path) -> None:
    path = _config(tmp_path, NEWER)
    save_engine_kind("native", path)
    text = path.read_text(encoding="utf-8")
    assert "verify_budget_usd: 2.5" in text
    assert "autonomy.verify_budget_usd" in load_config(path).unknown_keys


def test_the_names_survive_a_copy(tmp_path: Path) -> None:
    config = load_config(_config(tmp_path, NEWER))
    assert config.model_copy(update={"active_preset": "x"}).unknown_keys == config.unknown_keys
