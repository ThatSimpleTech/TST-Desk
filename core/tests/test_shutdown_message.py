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
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from tests.test_daemon_shutdown import (
    _read_log,
    _shutting_down_reasons,
    _spawn,
    _stop,
    _wait_until_serving,
)
from tstd.daemon import Daemon
from tstd.protocol import PROTOCOL_VERSION
from tstd.ws import create_port_file_path, read_port_file


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


async def _send_shutdown(data_dir: Path) -> None:
    info = read_port_file(data_dir)
    assert info is not None
    async with connect(f"ws://127.0.0.1:{info['port']}") as ws:
        await ws.send(
            json.dumps({"type": "hello", "token": info["token"], "version": PROTOCOL_VERSION})
        )
        ack = json.loads(await ws.recv())
        assert ack["type"] == "hello_ack"
        await ws.send(json.dumps({"type": "shutdown"}))


def test_shutdown_message_with_a_hung_phase_exits_inside_the_budget(tmp_path: Path) -> None:
    """TD-5021: the websocket path arms the 5 s budget too."""
    data_dir = tmp_path / "scratch"
    proc = _spawn([sys.executable, "-m", "tests.shutdown_child", str(data_dir)])
    try:
        _wait_until_serving(proc)
        asyncio.run(_send_shutdown(data_dir))
        sent = time.monotonic()
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            raise AssertionError("daemon still running 10s after shutdown") from None
        assert time.monotonic() - sent < 6.0
        assert code == 1
        text = _read_log(data_dir)
        assert "shutdown exceeded" in text
        assert _shutting_down_reasons(text) == ["websocket"]
    finally:
        _stop(proc)
