"""The model picker on a paired phone (TD-5242).

The phone runs the same window in a WebView, so the picker is the same
code. What differs is the peer: these cases are which of the model verbs a
phone may send, that it is told when the catalog changes, and that it
never makes this computer ask its own model servers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tests.test_add_model import _ask, _start, _stop
from tests.test_peer_policy import LOOPBACK_PLAIN, MACHINE, PHONE_VIEWER, PLAIN_REMOTE, _msg
from tests.test_preset_engine import _text as _catalog_with_grok
from tstd.mock import MockProvider, Script
from tstd.peer_policy import check
from tstd.protocol import PROTOCOL_VERSION

PHONE = "/phone"

# The verbs E54 added or changed, as a window sends them.
MAY = [
    ("switch_preset", {"name": "budget", "session_id": "s1", "make_default": True}),
    ("switch_preset", {"name": "budget", "make_default": True}),
    ("new_session", {"session_id": "s1", "preset": "grok"}),
    ("check_presets", {}),
    ("get_preset_roles", {"name": "budget"}),
    ("duplicate_preset", {"source": "local", "label": "Copy"}),
    ("set_preset_label", {"name": "local", "label": "Here"}),
    ("add_grok_preset", {}),
]


class TestWhatAPhoneMaySend:
    @pytest.mark.parametrize(("kind", "fields"), MAY)
    def test_a_phone_may_use_the_picker_and_the_model_list(
        self, kind: str, fields: dict[str, Any]
    ) -> None:
        for peer in (LOOPBACK_PLAIN, PHONE_VIEWER, PLAIN_REMOTE):
            assert check(_msg(kind, **fields), peer, bridged=False) is None, peer

    def test_only_the_computer_removes_a_model(self) -> None:
        remove = _msg("delete_preset", name="local")
        assert check(remove, LOOPBACK_PLAIN, bridged=False) is None
        for peer in (PHONE_VIEWER, PLAIN_REMOTE):
            refusal = check(remove, peer, bridged=False)
            assert refusal is not None and json.loads(refusal)["code"] == "loopback_only"

    @pytest.mark.parametrize(("kind", "fields"), [*MAY, ("delete_preset", {"name": "local"})])
    def test_another_computers_hub_may_send_none_of_them(
        self, kind: str, fields: dict[str, Any]
    ) -> None:
        if kind == "new_session":
            pytest.skip("new_session is a machine-peer verb of its own (TD-5015)")
        refusal = check(_msg(kind, **fields), MACHINE, bridged=False)
        assert refusal is not None
        assert json.loads(refusal)["code"] in ("not_allowed", "loopback_only")


async def _seeded(tmp_path: Path) -> tuple[Any, Any, Any]:
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(_catalog_with_grok(), encoding="utf-8")
    daemon, task, ws = await _start(data, MockProvider(default=Script(kind="text")))
    daemon.ws_server._is_remote_connection = lambda conn: (
        conn.request is not None and conn.request.path == PHONE
    )
    # No Tailscale listener in a test: mint the token one would have made,
    # so a connection on /phone signs in the way the older token way does.
    daemon.ws_server._mint_remote_token()
    return daemon, task, ws


async def _phone(daemon: Any) -> Any:
    ws = await connect(f"ws://127.0.0.1:{daemon.ws_server.port}{PHONE}")
    hello = {"type": "hello", "token": daemon.ws_server.remote_token, "version": PROTOCOL_VERSION}
    await ws.send(json.dumps(hello))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack", ack
    return ws


class TestOverTheWire:
    async def test_a_phone_gets_the_sections_engines_and_slots(self, tmp_path: Path) -> None:
        daemon, task, ws = await _seeded(tmp_path)
        phone = await _phone(daemon)
        try:
            state = await _ask(phone, {"type": "get_setup_state"}, "setup_state")
            assert state["preset_groups"]["local"] == "Local"
            assert state["preset_engines"] == {"grok": "grok"}
            assert state["picker_groups"] == ["Local", "API", "Grok"]
            # What a remote peer's setup_state always leaves out.
            assert "remote_devices" not in state and "remote_port" not in state
        finally:
            await phone.close()
            await _stop(task, ws)

    async def test_a_phone_is_told_when_the_window_changes_the_catalog(
        self, tmp_path: Path
    ) -> None:
        daemon, task, ws = await _seeded(tmp_path)
        phone = await _phone(daemon)
        try:
            await _ask(
                ws,
                {"type": "switch_preset", "name": "budget", "make_default": True},
                "setup_state",
            )
            told = await _ask(phone, {"type": "ping"}, "setup_state")
            assert told["type"] == "setup_state" and told["active_preset"] == "budget"
            assert "remote_devices" not in told and "remote_bind" not in told
        finally:
            await phone.close()
            await _stop(task, ws)

    async def test_the_window_is_told_when_a_phone_changes_it(self, tmp_path: Path) -> None:
        daemon, task, ws = await _seeded(tmp_path)
        phone = await _phone(daemon)
        try:
            reply = await _ask(
                phone,
                {"type": "switch_preset", "name": "budget", "make_default": True},
                "setup_state",
            )
            assert reply["type"] == "setup_state" and reply["active_preset"] == "budget"
            told = dict(json.loads(await asyncio.wait_for(ws.recv(), 5)))
            assert told["type"] == "setup_state" and told["active_preset"] == "budget"
            # The computer's own window still gets its full state.
            assert "remote_devices" in told
        finally:
            await phone.close()
            await _stop(task, ws)

    async def test_a_phone_never_makes_the_computer_ask_its_model_servers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[list[str]] = []

        async def answers(urls: list[str]) -> dict[str, bool]:
            asked.append(list(urls))
            return {url: False for url in urls}

        monkeypatch.setattr("tstd.daemon.loopback_answers", answers)
        monkeypatch.setattr("tstd.daemon._probe_grok", lambda _configured: (None, False))
        daemon, task, ws = await _seeded(tmp_path)
        phone = await _phone(daemon)
        try:
            far = await _ask(phone, {"type": "check_presets"}, "preset_readiness")
            rows = {row["preset"]: row["status"] for row in far["rows"]}
            assert asked == []
            # Keys and the Grok CLI are still reported; a server is not judged.
            assert rows["local"] == "ready" and rows["vllm"] == "ready"
            assert rows["grok"] == "no_grok" and rows["tst-default"] == "no_key"

            near = await _ask(ws, {"type": "check_presets"}, "preset_readiness")
            assert len(asked) == 1
            assert {r["preset"]: r["status"] for r in near["rows"]}["local"] == "server_down"
        finally:
            await phone.close()
            await _stop(task, ws)

    async def test_a_phone_cannot_remove_a_model(self, tmp_path: Path) -> None:
        daemon, task, ws = await _seeded(tmp_path)
        phone = await _phone(daemon)
        try:
            text = daemon.config_path.read_text(encoding="utf-8")
            reply = await _ask(phone, {"type": "delete_preset", "name": "local"}, "setup_state")
            assert reply["type"] == "error" and reply["code"] == "loopback_only"
            assert daemon.config_path.read_text(encoding="utf-8") == text
        finally:
            await phone.close()
            await _stop(task, ws)
