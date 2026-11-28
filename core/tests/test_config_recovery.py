"""A config.yaml that no longer loads never stops the start (SETMODEL-04).

Never touches the live config: every test works in tmp_path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.machine_helpers import Fleet, ask, window
from tstd.config import default_config_yaml, load_config
from tstd.config_recovery import last_good_path, load_with_recovery, restore_last_good
from tstd.config_write import write_config

GOOD = default_config_yaml()
BROKEN = GOOD + "\nsession: [unclosed\n"


def _config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_good_load_keeps_a_last_good_copy(tmp_path: Path) -> None:
    path = _config(tmp_path, GOOD)
    _, trouble = load_with_recovery(load_config, path)
    assert trouble is None
    assert last_good_path(path).read_text(encoding="utf-8") == GOOD


def test_a_write_that_lands_updates_the_copy(tmp_path: Path) -> None:
    path = _config(tmp_path, GOOD)
    changed = GOOD + "\n# a comment the user added\n"
    write_config(path, changed)
    assert last_good_path(path).read_text(encoding="utf-8") == changed


@pytest.mark.parametrize("text", [BROKEN, ""])
def test_a_broken_file_runs_on_the_last_good_copy_and_is_left_alone(
    tmp_path: Path, text: str
) -> None:
    path = _config(tmp_path, GOOD)
    load_with_recovery(load_config, path)
    path.write_text(text, encoding="utf-8")
    config, trouble = load_with_recovery(load_config, path)
    assert trouble is not None and trouble.running_on == "last_good" and trouble.error
    assert config is not None
    assert path.read_text(encoding="utf-8") == text  # untouched
    assert last_good_path(path).read_text(encoding="utf-8") == GOOD


def test_with_no_copy_it_runs_on_the_shipped_defaults(tmp_path: Path) -> None:
    path = _config(tmp_path, BROKEN)
    _, trouble = load_with_recovery(load_config, path)
    assert trouble is not None and trouble.running_on == "defaults"
    assert not last_good_path(path).exists()


def test_restore_puts_the_copy_back_and_keeps_the_broken_file(tmp_path: Path) -> None:
    path = _config(tmp_path, GOOD)
    load_with_recovery(load_config, path)
    path.write_text(BROKEN, encoding="utf-8")
    kept = restore_last_good(path)
    assert kept is not None and kept.startswith("config.yaml.damaged-")
    assert (tmp_path / kept).read_text(encoding="utf-8") == BROKEN
    assert path.read_text(encoding="utf-8") == GOOD
    assert restore_last_good(tmp_path / "other.yaml") is None


@pytest.fixture
async def fleet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fleet]:
    made = Fleet(monkeypatch)
    try:
        yield made
    finally:
        await made.close()


async def test_the_daemon_starts_says_why_and_restores(tmp_path: Path, fleet: Fleet) -> None:
    data = tmp_path / "d"
    data.mkdir()
    path = _config(data, GOOD)
    load_with_recovery(load_config, path)
    path.write_text(BROKEN, encoding="utf-8")
    daemon = await fleet.start(data)
    async with window(daemon) as ws:
        state = await ask(ws, {"type": "get_setup_state"}, "setup_state")
        assert state["config_running_on"] == "last_good"
        assert state["config_error"]
        restored = await ask(ws, {"type": "restore_last_good_config"}, "setup_state")
    assert restored["config_error"] is None
    assert path.read_text(encoding="utf-8") == GOOD
    assert list(data.glob("config.yaml.damaged-*"))
