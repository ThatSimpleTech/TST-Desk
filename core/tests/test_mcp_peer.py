"""A paired phone and the MCP page, at the socket (TD-5279).

A stdio MCP server is a command the daemon runs. On main a paired phone
could add one: the verb was open to it and the handler saved and started
whatever it was sent. Now a phone is refused unless the person at the
computer switched "MCP servers" on for phones, and when a phone does use
that, the computer's window is told and the server remembers its origin.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from websockets.asyncio.client import connect

from tests.test_add_model import _ask, _stop
from tests.test_mcp_secrets import MemoryKeychain, memory_keychain  # noqa: F401
from tests.test_models_on_phone import PHONE, _seeded
from tests.test_models_on_phone import _phone as _legacy_token_client
from tstd import phone_permissions as perms
from tstd.config import load_config
from tstd.protocol import PROTOCOL_VERSION

SERVER = {"type": "set_mcp_server", "id": "walk", "transport": "stdio", "command": ["true"]}
GRANT = {"type": "set_phone_permission", "capability": "mcp_servers", "enabled": True}
VALUE = {
    "type": "set_mcp_secret",
    "server_id": "walk",
    "target": "env",
    "name": "TOKEN",
    "value": "a-test-value-0001",
}


async def _pair(daemon: Any, name: str = "Pixel 8", kind: str = "phone") -> tuple[Any, str]:
    """A connection signed in with a paired device's own key, and the device id."""
    device, key = daemon.remote_devices.add(name, kind=kind)
    ws = await connect(f"ws://127.0.0.1:{daemon.ws_server.port}{PHONE}")
    await ws.send(json.dumps({"type": "hello", "token": key, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack", ack
    return ws, device.id


async def _drain(ws: Any) -> list[dict[str, Any]]:
    """Everything already on its way to *ws*: broadcasts that are not a reply."""
    seen: list[dict[str, Any]] = []
    with contextlib.suppress(TimeoutError):
        while True:
            seen.append(json.loads(await asyncio.wait_for(ws.recv(), 0.3)))
    return seen


async def _fresh(ws: Any, msg: dict[str, Any], want: str = "setup_state") -> dict[str, Any]:
    await _drain(ws)
    return await _ask(ws, msg, want)


def _spy_on_reload(daemon: Any, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = daemon._mcp.reload

    async def reload(config: Any) -> Any:
        calls.append(len(config.servers))
        return await real(config)

    monkeypatch.setattr(daemon._mcp, "reload", reload)
    return calls


async def test_a_phone_cannot_add_an_mcp_server_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    reloads = _spy_on_reload(daemon, monkeypatch)
    try:
        text = daemon.config_path.read_text(encoding="utf-8")
        # On main this ran: the marker appeared on the computer.
        marker = tmp_path / "ran-on-the-computer"
        hostile = {**SERVER, "command": ["sh", "-c", f"touch {marker}"]}
        reply = await _fresh(phone, hostile)
        assert reply["type"] == "error" and reply["code"] == "phone_permission_off"
        assert reply["in_reply_to"] == "set_mcp_server"
        assert "Settings, Phones and devices on the computer" in reply["message"]
        # Nothing saved, nothing started, nothing recorded.
        assert daemon.config_path.read_text(encoding="utf-8") == text
        assert load_config(daemon.config_path).mcp.servers == {}
        assert reloads == []
        assert not perms.origins_path(daemon.data_dir).exists()
        await asyncio.sleep(0.3)
        assert not marker.exists(), "a phone made this computer run a command"
        # And the phone is told the switch is off, so its page can say why.
        state = await _fresh(phone, {"type": "get_setup_state"})
        assert state["phone_permissions"]["mcp_servers"] == {"enabled": False, "expires_at": None}
        assert not any(p["enabled"] for p in state["phone_permissions"].values())
    finally:
        await phone.close()
        await _stop(task, ws)


async def test_a_phone_cannot_change_remove_or_set_a_value_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    try:
        saved = await _fresh(ws, SERVER)
        assert [row["id"] for row in saved["mcp_servers"]] == ["walk"]
        text = daemon.config_path.read_text(encoding="utf-8")
        reloads = _spy_on_reload(daemon, monkeypatch)
        attempts = [
            {**SERVER, "command": ["sh", "-c", "id"]},
            {**SERVER, "enabled": False},
            {"type": "delete_mcp_server", "id": "walk"},
            VALUE,
            {"type": "delete_mcp_secret", "server_id": "walk", "target": "env", "name": "TOKEN"},
        ]
        for attempt in attempts:
            reply = await _fresh(phone, attempt)
            assert reply["type"] == "error", attempt["type"]
            assert reply["code"] == "phone_permission_off", attempt["type"]
        assert daemon.config_path.read_text(encoding="utf-8") == text
        assert load_config(daemon.config_path).mcp.servers["walk"].command == ["true"]
        assert reloads == []
    finally:
        await phone.close()
        await _stop(task, ws)


async def test_a_phone_cannot_grant_itself(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    token_client = await _legacy_token_client(daemon)
    try:
        for peer in (phone, token_client):
            reply = await _fresh(peer, GRANT)
            assert reply["type"] == "error" and reply["code"] == "loopback_only"
            assert reply["in_reply_to"] == "set_phone_permission"
        assert not perms.permissions_path(daemon.data_dir).exists()
        assert daemon.phone_store.live() == frozenset()
        # Still refused afterwards.
        assert (await _fresh(phone, SERVER))["code"] == "phone_permission_off"
    finally:
        await token_client.close()
        await phone.close()
        await _stop(task, ws)


async def test_a_phone_can_add_one_after_the_computer_grants(
    tmp_path: Path,
    memory_keychain: MemoryKeychain,  # noqa: F811
) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, device_id = await _pair(daemon)
    try:
        granted = await _fresh(ws, GRANT)
        assert granted["type"] == "setup_state"
        assert granted["phone_permissions"]["mcp_servers"] == {"enabled": True, "expires_at": None}
        on_disk = yaml.safe_load(
            perms.permissions_path(daemon.data_dir).read_text(encoding="utf-8")
        )
        assert on_disk["grants"]["mcp_servers"]["enabled"] is True
        # The phone hears the switch without asking.
        told = [e for e in await _drain(phone) if e["type"] == "setup_state"]
        assert told and told[-1]["phone_permissions"]["mcp_servers"]["enabled"] is True
        assert "remote_devices" not in told[-1]

        reply = await _fresh(phone, SERVER)
        assert reply["type"] == "setup_state"
        assert [row["id"] for row in reply["mcp_servers"]] == ["walk"]
        assert reply["mcp_servers"][0]["origin_device"] == "Pixel 8"
        assert load_config(daemon.config_path).mcp.servers["walk"].command == ["true"]

        # The computer's window is told which phone did what, and sees the new list.
        heard = await _drain(ws)
        notices = [e for e in heard if e["type"] == "phone_change"]
        assert notices == [
            {
                "type": "phone_change",
                "seq": 1,
                "device_name": "Pixel 8",
                "capability": "mcp_servers",
                "summary": "added MCP server 'walk': true",
            }
        ]
        states = [e for e in heard if e["type"] == "setup_state"]
        assert states and [row["id"] for row in states[-1]["mcp_servers"]] == ["walk"]
        device = next(d for d in states[-1]["remote_devices"] if d["id"] == device_id)
        assert device["mcp_servers_added"] == ["walk"]

        # The origin is on file, in the data dir and not in config.yaml.
        origins = perms.load_origins(daemon.data_dir)
        assert origins["walk"].device_id == device_id
        assert "origin" not in daemon.config_path.read_text(encoding="utf-8")

        # A value set from the phone is announced by name, never by value.
        stored = await _fresh(phone, VALUE)
        assert stored["type"] == "setup_state", stored
        notices = [e for e in await _drain(ws) if e["type"] == "phone_change"]
        assert [n["summary"] for n in notices] == [
            "stored a value for variable 'TOKEN' of MCP server 'walk'."
        ]
        assert "a-test-value-0001" not in json.dumps(notices)
    finally:
        await phone.close()
        await _stop(task, ws)


async def test_a_phone_added_server_never_gets_allow_remote(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    try:
        await _fresh(ws, GRANT)
        frame = {**SERVER, "allow_remote": True}
        reply = await _fresh(phone, frame)
        # The verb has no such field; whatever the reply, the opt-in stays off.
        if reply["type"] == "setup_state":
            assert load_config(daemon.config_path).mcp.servers["walk"].allow_remote is False
        else:
            assert load_config(daemon.config_path).mcp.servers == {}
    finally:
        await phone.close()
        await _stop(task, ws)


async def test_a_grant_is_for_paired_phones_only(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    token_client = await _legacy_token_client(daemon)
    machine, _ = await _pair(daemon, "Other computer", kind="machine")
    try:
        await _fresh(ws, GRANT)
        text = daemon.config_path.read_text(encoding="utf-8")
        reply = await _fresh(token_client, SERVER)
        assert reply["type"] == "error" and reply["code"] == "phone_permission_off"
        reply = await _fresh(machine, SERVER)
        assert reply["type"] == "error" and reply["code"] == "not_allowed"
        assert daemon.config_path.read_text(encoding="utf-8") == text
    finally:
        await machine.close()
        await token_client.close()
        await _stop(task, ws)


async def test_a_timed_grant_runs_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    clock = [datetime(2026, 11, 20, 18, 0, 0, tzinfo=UTC)]
    monkeypatch.setattr(perms, "utc_now", lambda: clock[0])
    try:
        granted = await _fresh(ws, {**GRANT, "expires_in_seconds": 3600})
        assert granted["phone_permissions"]["mcp_servers"] == {
            "enabled": True,
            "expires_at": "2026-11-20T19:00:00Z",
        }
        assert (await _fresh(phone, SERVER))["type"] == "setup_state"
        clock[0] += timedelta(hours=1, seconds=1)
        reply = await _fresh(phone, {**SERVER, "command": ["false"]})
        assert reply["type"] == "error" and reply["code"] == "phone_permission_off"
        assert load_config(daemon.config_path).mcp.servers["walk"].command == ["true"]
        state = await _fresh(ws, {"type": "get_setup_state"})
        assert state["phone_permissions"]["mcp_servers"]["enabled"] is False
    finally:
        await phone.close()
        await _stop(task, ws)


async def test_the_windows_are_told_when_a_timed_grant_runs_out(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    try:
        await _fresh(ws, {**GRANT, "expires_in_seconds": 1})
        await _drain(phone)
        for peer in (ws, phone):
            for _ in range(40):
                event = json.loads(await asyncio.wait_for(peer.recv(), 10))
                if event["type"] == "setup_state":
                    break
            assert event["phone_permissions"]["mcp_servers"]["enabled"] is False
    finally:
        await phone.close()
        await _stop(task, ws)


async def test_a_bad_grant_is_refused(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    try:
        unknown = await _fresh(ws, {**GRANT, "capability": "everything"})
        assert unknown["type"] == "error" and unknown["code"] == "bad_request"
        too_long = await _fresh(ws, {**GRANT, "expires_in_seconds": 8 * 24 * 3600})
        assert too_long["type"] == "error" and too_long["code"] == "bad_request"
        assert daemon.phone_store.live() == frozenset()
        assert not perms.permissions_path(daemon.data_dir).exists()
    finally:
        await _stop(task, ws)


async def test_turning_the_grant_off_keeps_existing_servers(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    try:
        await _fresh(ws, GRANT)
        assert (await _fresh(phone, SERVER))["type"] == "setup_state"
        off = await _fresh(ws, {**GRANT, "enabled": False})
        assert off["phone_permissions"]["mcp_servers"]["enabled"] is False
        # As the disclosure says: it stops new changes, it removes nothing.
        assert [row["id"] for row in off["mcp_servers"]] == ["walk"]
        assert off["mcp_servers"][0]["origin_device"] == "Pixel 8"
        assert load_config(daemon.config_path).mcp.servers["walk"].command == ["true"]
        reply = await _fresh(phone, {"type": "delete_mcp_server", "id": "walk"})
        assert reply["type"] == "error" and reply["code"] == "phone_permission_off"
    finally:
        await phone.close()
        await _stop(task, ws)


async def test_a_grant_survives_a_restart_of_the_store(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    try:
        await _fresh(ws, GRANT)
        from tstd.phone_store import PhoneStore

        assert PhoneStore(daemon.data_dir).live() == {"mcp_servers"}
    finally:
        await _stop(task, ws)


async def test_removing_a_device_reports_and_can_remove_what_it_added(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, device_id = await _pair(daemon)
    try:
        await _fresh(ws, GRANT)
        await _fresh(ws, {**SERVER, "id": "mine"})
        assert (await _fresh(phone, SERVER))["type"] == "setup_state"
        state = await _fresh(ws, {"type": "get_setup_state"})
        device = next(d for d in state["remote_devices"] if d["id"] == device_id)
        assert device["mcp_servers_added"] == ["walk"]

        # Removing the phone alone leaves its server, still marked.
        kept = await _fresh(ws, {"type": "remove_remote_device", "device_id": device_id})
        assert sorted(row["id"] for row in kept["mcp_servers"]) == ["mine", "walk"]
        walk = next(row for row in kept["mcp_servers"] if row["id"] == "walk")
        assert walk["origin_device"] == "Pixel 8"
        assert perms.load_origins(daemon.data_dir)["walk"].device_id == device_id
    finally:
        with contextlib.suppress(Exception):
            await phone.close()
        await _stop(task, ws)


async def test_removing_a_device_with_what_it_added(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, device_id = await _pair(daemon)
    try:
        await _fresh(ws, GRANT)
        await _fresh(ws, {**SERVER, "id": "mine"})
        assert (await _fresh(phone, SERVER))["type"] == "setup_state"
        gone = await _fresh(
            ws, {"type": "remove_remote_device", "device_id": device_id, "remove_added": True}
        )
        assert gone["type"] == "setup_state"
        # The phone's server goes; the computer's own stays.
        assert [row["id"] for row in gone["mcp_servers"]] == ["mine"]
        assert list(load_config(daemon.config_path).mcp.servers) == ["mine"]
        assert perms.load_origins(daemon.data_dir) == {}
        assert gone["remote_devices"] == []
    finally:
        with contextlib.suppress(Exception):
            await phone.close()
        await _stop(task, ws)


async def test_a_phone_forgetting_itself_cannot_remove_servers(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, device_id = await _pair(daemon)
    try:
        await _fresh(ws, GRANT)
        assert (await _fresh(phone, SERVER))["type"] == "setup_state"
        await _fresh(ws, {**GRANT, "enabled": False})
        await _drain(phone)
        await phone.send(
            json.dumps(
                {"type": "remove_remote_device", "device_id": device_id, "remove_added": True}
            )
        )
        await asyncio.sleep(0.5)
        # The grant is off: the flag from a phone removes nothing.
        assert list(load_config(daemon.config_path).mcp.servers) == ["walk"]
        assert daemon.remote_devices.summaries() == []
    finally:
        with contextlib.suppress(Exception):
            await phone.close()
        await _stop(task, ws)


async def test_doctor_warns_while_a_phone_permission_is_on(tmp_path: Path) -> None:
    _daemon, task, ws = await _seeded(tmp_path)
    try:
        report = await _fresh(ws, {"type": "run_diagnostics"}, "diagnostics_report")
        assert "phone_permissions" not in [c["name"] for c in report["checks"]]
        await _fresh(ws, GRANT)
        report = await _fresh(ws, {"type": "run_diagnostics"}, "diagnostics_report")
        row = next(c for c in report["checks"] if c["name"] == "phone_permissions")
        assert row["status"] == "warn"
        assert row["detail"] == "paired phones may change: MCP servers ON (until turned off)"
    finally:
        await _stop(task, ws)


async def test_a_phone_still_sees_the_list(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    try:
        await _fresh(ws, SERVER)
        state = await _fresh(phone, {"type": "get_setup_state"})
        assert [row["id"] for row in state["mcp_servers"]] == ["walk"]
        assert state["mcp_servers"][0]["command"] == ["true"]
    finally:
        await phone.close()
        await _stop(task, ws)


async def test_the_computer_can_still_do_all_of_it(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    try:
        added = await _fresh(ws, SERVER)
        assert added["type"] == "setup_state"
        assert added["mcp_servers"][0]["origin_device"] == ""
        assert load_config(daemon.config_path).mcp.servers["walk"].command == ["true"]
        # A change at the computer is no phone change: nobody is told, nothing recorded.
        assert [e for e in await _drain(ws) if e["type"] == "phone_change"] == []
        assert not perms.origins_path(daemon.data_dir).exists()
        gone = await _fresh(ws, {"type": "delete_mcp_server", "id": "walk"})
        assert gone["type"] == "setup_state" and gone["mcp_servers"] == []
    finally:
        await _stop(task, ws)
