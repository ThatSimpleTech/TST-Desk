"""Only the computer changes its MCP servers (TD-5276).

A stdio MCP server is a command the daemon runs. A paired phone could add
one: the verb was open to it and the handler saved and started whatever it
was sent. These cases are a phone trying, at the socket, and the computer's
own window still being able to.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.test_add_model import _ask, _stop
from tests.test_models_on_phone import _phone, _seeded
from tstd.config import load_config

SERVER = {"type": "set_mcp_server", "id": "walk", "transport": "stdio", "command": ["true"]}


def _spy_on_reload(daemon: Any, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = daemon._mcp.reload

    async def reload(config: Any) -> Any:
        calls.append(len(config.servers))
        return await real(config)

    monkeypatch.setattr(daemon._mcp, "reload", reload)
    return calls


async def test_a_phone_cannot_add_a_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone = await _phone(daemon)
    reloads = _spy_on_reload(daemon, monkeypatch)
    try:
        text = daemon.config_path.read_text(encoding="utf-8")
        # Before the fix this ran: the marker appeared on the computer.
        marker = tmp_path / "ran-on-the-computer"
        hostile = {**SERVER, "command": ["sh", "-c", f"touch {marker}"]}
        reply = await _ask(phone, hostile, "setup_state")
        assert reply["type"] == "error" and reply["code"] == "loopback_only"
        assert reply["in_reply_to"] == "set_mcp_server"
        # Nothing saved, nothing started.
        assert daemon.config_path.read_text(encoding="utf-8") == text
        assert load_config(daemon.config_path).mcp.servers == {}
        assert reloads == []
        await asyncio.sleep(0.3)
        assert not marker.exists(), "a phone made this computer run a command"
    finally:
        await phone.close()
        await _stop(task, ws)


async def test_a_phone_cannot_change_remove_or_set_a_value_on_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone = await _phone(daemon)
    try:
        saved = await _ask(ws, SERVER, "setup_state")
        assert [row["id"] for row in saved["mcp_servers"]] == ["walk"]
        text = daemon.config_path.read_text(encoding="utf-8")
        reloads = _spy_on_reload(daemon, monkeypatch)
        attempts = [
            {**SERVER, "command": ["sh", "-c", "id"]},
            {**SERVER, "enabled": False},
            {"type": "delete_mcp_server", "id": "walk"},
            {
                "type": "set_mcp_secret",
                "server_id": "walk",
                "target": "env",
                "name": "TOKEN",
                "value": "v",
            },
            {"type": "delete_mcp_secret", "server_id": "walk", "target": "env", "name": "TOKEN"},
        ]
        for attempt in attempts:
            reply = await _ask(phone, attempt, "setup_state")
            assert reply["type"] == "error", attempt["type"]
            assert reply["code"] == "loopback_only", attempt["type"]
        assert daemon.config_path.read_text(encoding="utf-8") == text
        assert load_config(daemon.config_path).mcp.servers["walk"].command == ["true"]
        assert reloads == []
    finally:
        await phone.close()
        await _stop(task, ws)


async def test_a_phone_still_sees_the_list(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone = await _phone(daemon)
    try:
        await _ask(ws, SERVER, "setup_state")
        state = await _ask(phone, {"type": "get_setup_state"}, "setup_state")
        assert [row["id"] for row in state["mcp_servers"]] == ["walk"]
        assert state["mcp_servers"][0]["command"] == ["true"]
    finally:
        await phone.close()
        await _stop(task, ws)


async def test_the_computers_own_window_still_manages_them(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    try:
        added = await _ask(ws, SERVER, "setup_state")
        assert added["type"] == "setup_state"
        assert load_config(daemon.config_path).mcp.servers["walk"].command == ["true"]
        gone = await _ask(ws, {"type": "delete_mcp_server", "id": "walk"}, "setup_state")
        assert gone["type"] == "setup_state" and gone["mcp_servers"] == []
    finally:
        await _stop(task, ws)
