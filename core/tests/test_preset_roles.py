"""Settings → Model edits the entry you choose, not only the active one (TD-5241).

``setup_state`` describes the active entry's roles. ``get_preset_roles``
gives the same four maps for any entry, so the page can show and edit one
without first making it the model new chats use.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_add_model import _ask, _start, _stop
from tests.test_preset_engine import _text as _catalog_with_grok
from tstd.config import load_config
from tstd.mock import MockProvider, Script
from tstd.peer_policy import MACHINE_PEER_VERBS

ROLE_MAPS = ("tier_slugs", "tier_credentials", "tier_loopback", "tier_cards")


async def _seeded(tmp_path: Path) -> tuple:  # type: ignore[type-arg]
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(_catalog_with_grok(), encoding="utf-8")
    return await _start(data, MockProvider(default=Script(kind="text")))


async def test_an_entrys_roles_are_what_setup_state_says_when_it_is_active(
    tmp_path: Path,
) -> None:
    _daemon, task, ws = await _seeded(tmp_path)
    try:
        before = await _ask(ws, {"type": "get_setup_state"}, "setup_state")
        assert before["active_preset"] == "tst-default"
        roles = await _ask(ws, {"type": "get_preset_roles", "name": "budget"}, "preset_roles")
        assert roles["type"] == "preset_roles" and roles["preset"] == "budget"
        assert set(roles["tier_slugs"]) == {"brain", "worker", "validator"}
        # Asking changed nothing: the active entry is still the active one.
        again = await _ask(ws, {"type": "get_setup_state"}, "setup_state")
        assert again["active_preset"] == "tst-default"
        assert {k: again[k] for k in ROLE_MAPS} == {k: before[k] for k in ROLE_MAPS}

        active = await _ask(ws, {"type": "set_preset", "name": "budget"}, "setup_state")
        assert {k: roles[k] for k in ROLE_MAPS} == {k: active[k] for k in ROLE_MAPS}
    finally:
        await _stop(task, ws)


async def test_a_role_on_this_computer_and_one_at_a_provider(tmp_path: Path) -> None:
    _daemon, task, ws = await _seeded(tmp_path)
    try:
        local = await _ask(ws, {"type": "get_preset_roles", "name": "local"}, "preset_roles")
        assert set(local["tier_loopback"].values()) == {True}
        assert set(local["tier_credentials"].values()) == {None}
        # Discovered from the server each run: no model is named.
        assert set(local["tier_slugs"].values()) == {None}
        hosted = await _ask(ws, {"type": "get_preset_roles", "name": "tst-default"}, "preset_roles")
        assert set(hosted["tier_loopback"].values()) == {False}
        assert hosted["tier_credentials"]["brain"] == "openrouter"
        assert hosted["tier_cards"]["brain"]["context_window"] == 1000000
    finally:
        await _stop(task, ws)


async def test_an_edit_to_a_chosen_entry_shows_in_its_roles_and_not_the_active_ones(
    tmp_path: Path,
) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    try:
        before = await _ask(ws, {"type": "get_setup_state"}, "setup_state")
        ack = await _ask(
            ws,
            {
                "type": "set_tier_context_window",
                "preset": "local",
                "tier": "worker",
                "context_window": 49152,
            },
            "setup_state",
        )
        # The ack still describes the active entry, untouched.
        assert ack["active_preset"] == "tst-default"
        assert ack["tier_cards"] == before["tier_cards"]
        roles = await _ask(ws, {"type": "get_preset_roles", "name": "local"}, "preset_roles")
        assert roles["tier_cards"]["worker"]["context_window"] == 49152
        worker = load_config(daemon.config_path).presets["local"].worker
        assert worker is not None and worker.context_window == 49152
    finally:
        await _stop(task, ws)


async def test_an_entry_with_no_roles_has_none(tmp_path: Path) -> None:
    _daemon, task, ws = await _seeded(tmp_path)
    try:
        roles = await _ask(ws, {"type": "get_preset_roles", "name": "grok"}, "preset_roles")
        assert roles["preset"] == "grok"
        assert all(roles[key] == {} for key in ROLE_MAPS)
    finally:
        await _stop(task, ws)


async def test_an_unknown_entry_is_a_typed_error(tmp_path: Path) -> None:
    _daemon, task, ws = await _seeded(tmp_path)
    try:
        reply = await _ask(ws, {"type": "get_preset_roles", "name": "nope"}, "preset_roles")
        assert reply["type"] == "error" and reply["code"] == "unknown_preset"
    finally:
        await _stop(task, ws)


def test_another_computers_hub_may_not_send_it() -> None:
    assert "get_preset_roles" not in MACHINE_PEER_VERBS
