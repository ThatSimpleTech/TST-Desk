"""Tests for the WebSocket ``shutdown`` message (TD-1002).

The Tauri host shuts the daemon down cleanly by sending ``shutdown`` over
the authenticated WebSocket — one cross-platform path, no SIGTERM handling
split. On clean shutdown the port file must be removed so a supervising
host can tell a live daemon from a defunct one.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from tstd.daemon import Daemon
from tstd.protocol import PROTOCOL_VERSION
from tstd.ws import create_port_file_path


async def _wait_for_port_file(path: Path, seconds: float = 10.0) -> dict[str, Any]:
    try:
        async with asyncio.timeout(seconds):
            while True:
                if await asyncio.to_thread(path.exists):
                    info: dict[str, Any] = await asyncio.to_thread(
                        lambda: json.loads(path.read_text())
                    )
                    return info
                await asyncio.sleep(0.01)
    except TimeoutError as e:
        raise RuntimeError(f"port file never appeared at {path}") from e


class TestShutdownMessage:
    async def test_shutdown_message_stops_daemon_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            daemon = Daemon(data_dir=data_dir)
            runner = asyncio.create_task(daemon.run())

            port_file = create_port_file_path(data_dir)
            info = await _wait_for_port_file(port_file)

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

            # The daemon should stop cleanly of its own accord.
            await asyncio.wait_for(runner, timeout=5.0)

            # Clean shutdown removes the port file.
            assert not port_file.exists()
