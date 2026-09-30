"""Tests for the WebSocket ``shutdown`` message (TD-1002).

The Tauri host shuts the daemon down cleanly by sending ``shutdown`` over
the authenticated WebSocket — one cross-platform path, no SIGTERM handling
split. On clean shutdown the port file must be removed so a supervising
host can tell a live daemon from a defunct one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from tstd.daemon import Daemon
from tstd.protocol import PROTOCOL_VERSION
from tstd.ws import create_port_file_path


async def _wait_for_port_file(path: Path, seconds: float = 10.0) -> dict:
    try:
        async with asyncio.timeout(seconds):
            while True:
                if await asyncio.to_thread(path.exists):
                    return await asyncio.to_thread(lambda: json.loads(path.read_text()))
                await asyncio.sleep(0.01)
    except TimeoutError as e:
        raise RuntimeError(f"port file never appeared at {path}") from e


async def _shutdown_via_message(data_dir: Path, *, foreign_pid: int | None) -> Path:
    daemon = Daemon(data_dir=data_dir)
    runner = asyncio.create_task(daemon.run())
    port_file = create_port_file_path(data_dir)
    try:
        info = await _wait_for_port_file(port_file)
        if foreign_pid is not None:
            info["pid"] = foreign_pid
            port_file.write_text(json.dumps(info), encoding="utf-8")
        async with connect(f"ws://127.0.0.1:{info['port']}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "hello",
                        "token": info["token"],
                        "version": PROTOCOL_VERSION,
                    }
                )
            )
            ack = json.loads(await ws.recv())
            assert ack["type"] == "hello_ack"
            await ws.send(json.dumps({"type": "shutdown"}))
        await asyncio.wait_for(runner, timeout=5.0)
    finally:
        if not runner.done():
            daemon._shutdown_event.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(runner, timeout=5)
    return port_file


class TestShutdownMessage:
    async def test_shutdown_message_stops_daemon_cleanly(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            caplog.at_level(logging.INFO, logger="tstd.daemon"),
            tempfile.TemporaryDirectory() as tmp,
        ):
            port_file = await _shutdown_via_message(Path(tmp), foreign_pid=None)
            assert not port_file.exists()
        requested = [
            record
            for record in caplog.records
            if record.name == "tstd.daemon"
            and record.levelno == logging.INFO
            and record.getMessage() == "shutdown requested via websocket"
        ]
        assert requested

    async def test_shutdown_message_leaves_a_foreign_port_file(self) -> None:
        foreign = os.getpid() + 1
        with tempfile.TemporaryDirectory() as tmp:
            port_file = await _shutdown_via_message(Path(tmp), foreign_pid=foreign)
            kept = json.loads(port_file.read_text(encoding="utf-8"))
            assert kept["pid"] == foreign
