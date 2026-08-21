"""Coworker-mode persistence (TD-2902) and Settings toggle (TD-2905)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from tstd.coworker import (
    coworker_path,
    ensure_coworker,
    load_coworker,
    save_coworker,
)
from tstd.daemon import Daemon
from tstd.protocol import PROTOCOL_VERSION


class TestCoworkerPersist:
    def test_absent_is_on(self, tmp_path: Path) -> None:
        assert load_coworker(tmp_path) is True

    def test_round_trip(self, tmp_path: Path) -> None:
        save_coworker(tmp_path, False)
        assert load_coworker(tmp_path) is False
        save_coworker(tmp_path, True)
        assert load_coworker(tmp_path) is True

    def test_lands_in_user_data_not_workspace_config(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        workspace = tmp_path / "ws"
        (workspace / ".tst").mkdir(parents=True)
        workspace_config = workspace / ".tst" / "config.yaml"
        workspace_config.write_text("policy:\n  rules: []\n", encoding="utf-8")
        before = workspace_config.read_text(encoding="utf-8")

        save_coworker(data, True)

        assert coworker_path(data).exists()
        assert not (workspace / "coworker.yaml").exists()
        assert workspace_config.read_text(encoding="utf-8") == before

    def test_unreadable_or_junk_is_on(self, tmp_path: Path) -> None:
        path = coworker_path(tmp_path)
        path.write_text(":::: not yaml", encoding="utf-8")
        assert load_coworker(tmp_path) is True
        path.write_text("- just a list\n", encoding="utf-8")
        assert load_coworker(tmp_path) is True
        path.write_text("enabled: false\n", encoding="utf-8")
        assert load_coworker(tmp_path) is False
        path.write_text('enabled: "false"\n', encoding="utf-8")
        assert load_coworker(tmp_path) is False
        path.write_text("other: true\n", encoding="utf-8")
        assert load_coworker(tmp_path) is True
        path.write_text("", encoding="utf-8")
        assert load_coworker(tmp_path) is True

    def test_ensure_writes_default_on_once(self, tmp_path: Path) -> None:
        assert not coworker_path(tmp_path).exists()
        assert ensure_coworker(tmp_path) is True
        assert coworker_path(tmp_path).read_text(encoding="utf-8").find("enabled: true") >= 0
        save_coworker(tmp_path, False)
        assert ensure_coworker(tmp_path) is False


async def _connect_and_handshake(uri: str, token: str) -> Any:
    ws = await connect(uri)
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _start_daemon(tmp: Path) -> tuple[Daemon, asyncio.Task[Any]]:
    daemon = Daemon(data_dir=tmp)
    task = asyncio.create_task(daemon.run())
    for _ in range(50):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


async def _stop_daemon(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _ask(ws: Any, msg: dict[str, Any]) -> dict[str, Any]:
    await ws.send(json.dumps(msg))
    return dict(json.loads(await ws.recv()))


class TestCoworkerSettingsWire:
    async def test_setup_state_defaults_on(self, tmp_path: Path) -> None:
        daemon, task = await _start_daemon(tmp_path)
        try:
            assert daemon.coworker_enabled is True
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            resp = await _ask(ws, {"type": "get_setup_state"})
            assert resp["type"] == "setup_state"
            assert resp["coworker_enabled"] is True
            await ws.close()
        finally:
            await _stop_daemon(task)

    async def test_set_coworker_writes_what_load_coworker_reads(self, tmp_path: Path) -> None:
        daemon, task = await _start_daemon(tmp_path)
        try:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            resp = await _ask(ws, {"type": "set_coworker", "enabled": False})
            assert resp["type"] == "setup_state"
            assert resp["coworker_enabled"] is False
            assert load_coworker(tmp_path) is False
            text = coworker_path(tmp_path).read_text(encoding="utf-8")
            assert "enabled: false" in text
            resp = await _ask(ws, {"type": "set_coworker", "enabled": True})
            assert resp["coworker_enabled"] is True
            assert load_coworker(tmp_path) is True
            await ws.close()
        finally:
            await _stop_daemon(task)

    async def test_off_survives_restart(self, tmp_path: Path) -> None:
        daemon, task = await _start_daemon(tmp_path)
        try:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            await _ask(ws, {"type": "set_coworker", "enabled": False})
            await ws.close()
        finally:
            await _stop_daemon(task)

        daemon2, task2 = await _start_daemon(tmp_path)
        try:
            assert daemon2.coworker_enabled is False
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon2.ws_server.port}", daemon2.ws_server.token
            )
            resp = await _ask(ws, {"type": "get_setup_state"})
            assert resp["coworker_enabled"] is False
            await ws.close()
        finally:
            await _stop_daemon(task2)
