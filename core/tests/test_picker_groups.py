"""The picker's sections and quick-switch slots (TD-5236).

Local, API and Grok are sections of one list. The daemon says which
section each model is in, so the window never guesses from a URL; an entry
can name its own. A slot, 1 to 9, gives an entry a shortcut. And a change
to the catalog reaches every window of this computer, not only the one
that made it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tests.test_add_model import _ask, _start, _stop
from tests.test_preset_engine import _text as _catalog_with_grok
from tstd.config import PICKER_GROUPS, ConfigError, ModelConfig, load_config
from tstd.mock import MockProvider, Script
from tstd.preset_write import duplicate_preset
from tstd.protocol import PROTOCOL_VERSION

LAN = """\
  lan-box:
    label: The box next door
    group: Local
    slot: 2
    brain: &lan
      slug: house/model
      base_url: http://192.168.1.10:4000/v1
      credential: openrouter
      context_window: 65536
      max_output_tokens: 8192
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
    worker: *lan
    validator: *lan
  mine:
    label: Experiments
    group: "  Trying out  "
    brain: *lan
    worker: *lan
    validator: *lan
"""


def _text(extra: str = LAN) -> str:
    text = _catalog_with_grok()
    assert text.count("\npresets:\n") == 1
    return text.replace("\npresets:\n", "\npresets:\n" + extra, 1)


def _with(text: str, entry: str, block: str) -> str:
    header = f"\n  {entry}:\n"
    assert text.count(header) == 1, entry
    return text.replace(header, header + block, 1)


def _config(tmp_path: Path, text: str | None = None) -> ModelConfig:
    path = tmp_path / "config.yaml"
    path.write_text(_text() if text is None else text, encoding="utf-8")
    return load_config(path)


class TestTheSectionOfAnEntry:
    def test_the_daemon_reads_it_off_the_entry(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        # Every role on this computer.
        assert config.preset_group("local") == "Local"
        assert config.preset_group("vllm") == "Local"
        # A provider's server.
        assert config.preset_group("tst-default") == "API"
        assert config.preset_group("budget") == "API"
        # The Grok engine.
        assert config.preset_group("grok") == "Grok"
        assert config.preset_group("nope") == "API"

    def test_an_entry_can_say_its_own(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        # A server on the local network is not this computer: only the
        # person knows it is theirs, and here they said so.
        assert config.preset_group("lan-box") == "Local"
        assert config.presets["mine"].group == "Trying out"
        assert config.preset_group("mine") == "Trying out"

    def test_without_saying_so_a_server_on_the_network_is_api(self, tmp_path: Path) -> None:
        config = _config(tmp_path, _text(LAN.replace("    group: Local\n", "")))
        assert config.preset_group("lan-box") == "API"

    def test_a_key_that_points_a_local_role_at_a_provider_makes_it_api(
        self, tmp_path: Path
    ) -> None:
        # TD-1718: a bound key's host is where the request really goes.
        text = _text().replace(
            "      base_url: http://127.0.0.1:11434/v1\n",
            "      base_url: http://127.0.0.1:11434/v1\n      credential: openrouter\n",
        )
        config = _config(tmp_path, text)
        assert config.credentials["openrouter"].base_url is not None
        assert config.preset_group("local") == "API"

    def test_the_built_in_sections_come_in_this_order(self) -> None:
        assert PICKER_GROUPS == ("Local", "API", "Grok")

    @pytest.mark.parametrize("group", ['""', "'   '", "x" * 21])
    def test_a_blank_group_is_unset_and_a_long_one_is_refused(
        self, tmp_path: Path, group: str
    ) -> None:
        text = _with(_text(""), "budget", f"    group: {group}\n")
        if len(group) > 20:
            with pytest.raises(ConfigError, match=r"presets\.budget\.group"):
                _config(tmp_path, text)
            return
        if group == '""':
            with pytest.raises(ConfigError, match=r"presets\.budget\.group"):
                _config(tmp_path, text)
            return
        assert _config(tmp_path, text).presets["budget"].group is None


class TestSlots:
    def test_a_slot_is_one_to_nine(self, tmp_path: Path) -> None:
        assert _config(tmp_path).presets["lan-box"].slot == 2
        for bad in ("0", "10", "-1", "two"):
            with pytest.raises(ConfigError, match=r"presets\.budget\.slot"):
                _config(tmp_path, _with(_text(""), "budget", f"    slot: {bad}\n"))

    def test_no_two_entries_share_one(self, tmp_path: Path) -> None:
        text = _with(_text(), "budget", "    slot: 2\n")
        with pytest.raises(ConfigError) as caught:
            _config(tmp_path, text)
        message = str(caught.value)
        assert "slot 2 is already" in message
        assert "lan-box" in message and "budget" in message

    def test_a_copy_does_not_take_its_sources_slot(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(_text(), encoding="utf-8")
        duplicate_preset("lan-box", "lan-box-2", "The box, again", path)
        config = load_config(path)
        assert config.presets["lan-box"].slot == 2
        assert config.presets["lan-box-2"].slot is None
        # The section travels with the copy; the shortcut cannot.
        assert config.presets["lan-box-2"].group == "Local"


def _mock() -> MockProvider:
    return MockProvider(default=Script(kind="text"))


async def _seeded(tmp_path: Path) -> tuple[Any, Any, Any]:
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(_text(), encoding="utf-8")
    return await _start(data, _mock())


async def _second_window(daemon: Any) -> Any:
    ws = await connect(f"ws://127.0.0.1:{daemon.ws_server.port}")
    await ws.send(
        json.dumps({"type": "hello", "token": daemon.ws_server.token, "version": PROTOCOL_VERSION})
    )
    assert json.loads(await ws.recv())["type"] == "hello_ack"
    return ws


async def _next_setup_state(ws: Any) -> dict[str, Any]:
    for _ in range(40):
        event = dict(json.loads(await asyncio.wait_for(ws.recv(), 5)))
        if event["type"] == "setup_state":
            return event
    raise AssertionError("no setup_state reached the other window")


class TestOverTheWire:
    async def test_setup_state_names_every_models_section_and_each_slot(
        self, tmp_path: Path
    ) -> None:
        _daemon, task, ws = await _seeded(tmp_path)
        try:
            state = await _ask(ws, {"type": "get_setup_state"}, "setup_state")
            assert state["picker_groups"] == ["Local", "API", "Grok"]
            assert set(state["preset_groups"]) == set(state["presets"])
            assert state["preset_groups"]["local"] == "Local"
            assert state["preset_groups"]["lan-box"] == "Local"
            assert state["preset_groups"]["tst-default"] == "API"
            assert state["preset_groups"]["grok"] == "Grok"
            assert state["preset_groups"]["mine"] == "Trying out"
            assert state["preset_slots"] == {"lan-box": 2}
        finally:
            await _stop(task, ws)

    @pytest.mark.parametrize(
        ("message", "check"),
        [
            (
                {"type": "switch_preset", "name": "budget", "make_default": True},
                lambda s: s["active_preset"] == "budget",
            ),
            ({"type": "set_preset", "name": "budget"}, lambda s: s["active_preset"] == "budget"),
            (
                {"type": "duplicate_preset", "source": "local", "label": "Local two"},
                lambda s: "local-two" in s["presets"],
            ),
            (
                {"type": "set_preset_label", "name": "local", "label": "Here"},
                lambda s: s["preset_labels"]["local"] == "Here",
            ),
            ({"type": "delete_preset", "name": "mine"}, lambda s: "mine" not in s["presets"]),
        ],
    )
    async def test_a_change_in_one_window_reaches_the_other(
        self, tmp_path: Path, message: dict[str, Any], check: Any
    ) -> None:
        daemon, task, ws = await _seeded(tmp_path)
        other = await _second_window(daemon)
        try:
            reply = await _ask(ws, message, "setup_state")
            assert reply["type"] == "setup_state" and check(reply)
            # Unasked: the window that did nothing is told too.
            told = await _next_setup_state(other)
            assert check(told)
            assert told["preset_groups"] == reply["preset_groups"]
        finally:
            await other.close()
            await _stop(task, ws)

    async def test_switching_only_a_chat_tells_no_other_window_about_the_catalog(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        daemon, task, ws = await _seeded(tmp_path)
        other = await _second_window(daemon)
        try:
            opened = await _ask(
                ws, {"type": "open_workspace", "path": str(workspace)}, "session_state"
            )
            await _ask(
                ws,
                {"type": "switch_preset", "name": "budget", "session_id": opened["session_id"]},
                "session_list",
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(_next_setup_state(other), 0.5)
        finally:
            await other.close()
            await _stop(task, ws)
