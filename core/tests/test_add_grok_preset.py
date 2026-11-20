"""Put the Grok CLI on the model list from Settings → Engine (TD-5258).

One narrow verb writes an entry that is ``engine: grok`` and nothing else
(TD-5239). It is offered only where the Grok CLI is found, and once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.catalog_helpers import catalog_config_yaml
from tests.test_add_model import _ask, _start, _stop
from tstd.config import ConfigError, default_config_yaml, load_config
from tstd.mock import MockProvider, Script
from tstd.peer_policy import MACHINE_PEER_VERBS
from tstd.preset_write import add_grok_preset

DESCRIPTION = "The Grok CLI, with its own model"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestTheWriter:
    def test_into_the_shipped_empty_catalog(self, tmp_path: Path) -> None:
        shipped = default_config_yaml()
        path = _write(tmp_path, shipped)
        add_grok_preset("grok", "Grok", DESCRIPTION, path)
        block = (
            '\npresets:\n  grok:\n    label: "Grok"\n'
            f'    description: "{DESCRIPTION}"\n    engine: grok\n'
        )
        assert path.read_text(encoding="utf-8") == shipped.replace("\npresets: {}\n", block, 1)
        config = load_config(path)
        entry = config.presets["grok"]
        assert entry.engine == "grok" and entry.has_tiers is False
        assert entry.label == "Grok" and entry.description == DESCRIPTION
        # The only entry, so the loader makes it the one new chats use.
        assert config.active_preset == "grok" and config.has_model() is True
        assert config.requires_api_key() is False

    def test_after_the_last_entry_of_a_catalog_and_nothing_else_changes(
        self, tmp_path: Path
    ) -> None:
        before = catalog_config_yaml()
        path = _write(tmp_path, before)
        was_active = load_config(path).active_preset
        add_grok_preset("grok", "Grok", DESCRIPTION, path)
        after = path.read_text(encoding="utf-8").split("\n")
        start = after.index("  grok:")
        assert after[start : start + 4] == [
            "  grok:",
            '    label: "Grok"',
            f'    description: "{DESCRIPTION}"',
            "    engine: grok",
        ]
        assert "\n".join(after[:start] + after[start + 4 :]) == before
        config = load_config(path)
        assert list(config.presets)[-1] == "grok"
        assert config.active_preset == was_active

    def test_a_file_with_no_catalog_at_all(self, tmp_path: Path) -> None:
        path = _write(tmp_path, default_config_yaml().replace("\npresets: {}\n", "\n", 1))
        add_grok_preset("grok", "Grok", "", path)
        entry = load_config(path).presets["grok"]
        assert entry.engine == "grok" and entry.description is None

    def test_a_label_cannot_inject_yaml(self, tmp_path: Path) -> None:
        path = _write(tmp_path, default_config_yaml())
        add_grok_preset("grok", 'G"\n    engine: native', DESCRIPTION, path)
        entry = load_config(path).presets["grok"]
        assert entry.label == 'G"\n    engine: native' and entry.engine == "grok"

    def test_once_is_enough(self, tmp_path: Path) -> None:
        path = _write(tmp_path, default_config_yaml())
        add_grok_preset("grok", "Grok Build", DESCRIPTION, path)
        text = path.read_text(encoding="utf-8")
        with pytest.raises(ConfigError, match=r"already one of your models \(Grok Build\)"):
            add_grok_preset("grok-2", "Grok", DESCRIPTION, path)
        assert path.read_text(encoding="utf-8") == text

    @pytest.mark.parametrize(
        ("name", "label", "says"),
        [
            ("local", "Grok", "already exists"),
            ("Not A Slug", "Grok", "lowercase slug"),
            ("grok", "  ", "Give the model a name"),
        ],
    )
    def test_refusals_write_nothing(self, tmp_path: Path, name: str, label: str, says: str) -> None:
        path = _write(tmp_path, catalog_config_yaml())
        with pytest.raises(ConfigError, match=says):
            add_grok_preset(name, label, DESCRIPTION, path)
        assert path.read_text(encoding="utf-8") == catalog_config_yaml()


def _mock() -> MockProvider:
    return MockProvider(default=Script(kind="text"))


def _grok(monkeypatch: pytest.MonkeyPatch, *, found: bool) -> None:
    result = (Path("/usr/bin/grok"), True) if found else (None, False)
    monkeypatch.setattr("tstd.daemon._probe_grok", lambda _configured: result)


class TestOverTheWire:
    async def test_it_adds_the_entry_and_leaves_the_active_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _grok(monkeypatch, found=True)
        daemon, task, ws = await _start(tmp_path, _mock())
        try:
            before = await _ask(ws, {"type": "get_setup_state"}, "setup_state")
            assert before["preset_engines"] == {}
            state = await _ask(ws, {"type": "add_grok_preset"}, "setup_state")
            assert state["type"] == "setup_state"
            assert state["presets"] == sorted([*before["presets"], "grok"])
            assert state["preset_engines"] == {"grok": "grok"}
            assert state["preset_labels"]["grok"] == "Grok"
            assert state["preset_descriptions"]["grok"] == DESCRIPTION
            assert state["active_preset"] == before["active_preset"]
            assert state["engine"] == before["engine"]
            on_disk = load_config(daemon.config_path).presets["grok"]
            assert on_disk.engine == "grok" and on_disk.has_tiers is False
        finally:
            await _stop(task, ws)

    async def test_a_second_ask_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _grok(monkeypatch, found=True)
        daemon, task, ws = await _start(tmp_path, _mock())
        try:
            await _ask(ws, {"type": "add_grok_preset", "label": "Grok Build"}, "setup_state")
            text = daemon.config_path.read_text(encoding="utf-8")
            reply = await _ask(ws, {"type": "add_grok_preset"}, "setup_state")
            assert reply["type"] == "error" and reply["code"] == "bad_request"
            assert "already one of your models (Grok Build)" in reply["message"]
            assert daemon.config_path.read_text(encoding="utf-8") == text
        finally:
            await _stop(task, ws)

    async def test_it_is_refused_where_the_grok_cli_is_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _grok(monkeypatch, found=False)
        daemon, task, ws = await _start(tmp_path, _mock())
        try:
            text = daemon.config_path.read_text(encoding="utf-8")
            reply = await _ask(ws, {"type": "add_grok_preset"}, "setup_state")
            assert reply["type"] == "error" and reply["code"] == "grok_unavailable"
            assert "not found" in reply["message"]
            assert daemon.config_path.read_text(encoding="utf-8") == text
        finally:
            await _stop(task, ws)


def test_another_computers_hub_may_not_send_it() -> None:
    assert "add_grok_preset" not in MACHINE_PEER_VERBS
