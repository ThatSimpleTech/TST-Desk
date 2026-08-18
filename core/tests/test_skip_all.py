"""Skip-all approvals (TD-804).

The settings toggle is a machine-wide bit in the user data dir. These
tests cover the daemon wire: setup_state reports it, flipping it
persists across restart, and it is off by default.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from tstd.daemon import Daemon
from tstd.policy import load_skip_all
from tstd.protocol import PROTOCOL_VERSION


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


class TestSkipAllWire:
    async def test_setup_state_defaults_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                assert daemon.skip_all_approvals is False
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "get_setup_state"})
                assert resp["type"] == "setup_state"
                assert resp["skip_all_approvals"] is False
                await ws.close()
            finally:
                await _stop_daemon(task)

    async def test_toggle_persists_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            daemon, task = await _start_daemon(data)
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "set_skip_all_approvals", "enabled": True})
                assert resp["type"] == "setup_state"
                assert resp["skip_all_approvals"] is True
                assert load_skip_all(data) is True
                assert (data / "approvals.yaml").exists()
                await ws.close()
            finally:
                await _stop_daemon(task)

            daemon2, task2 = await _start_daemon(data)
            try:
                assert daemon2.skip_all_approvals is True
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon2.ws_server.port}", daemon2.ws_server.token
                )
                resp = await _ask(ws, {"type": "get_setup_state"})
                assert resp["skip_all_approvals"] is True
                await ws.close()
            finally:
                await _stop_daemon(task2)

    async def test_turning_off_writes_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            daemon, task = await _start_daemon(data)
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                await _ask(ws, {"type": "set_skip_all_approvals", "enabled": True})
                resp = await _ask(ws, {"type": "set_skip_all_approvals", "enabled": False})
                assert resp["skip_all_approvals"] is False
                assert load_skip_all(data) is False
                await ws.close()
            finally:
                await _stop_daemon(task)
