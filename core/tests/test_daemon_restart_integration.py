"""End-to-end daemon crash + restart with the session list intact (TD-1002).

Spawns the real ``tstd`` daemon as a child process, opens a session over
the WebSocket, SIGKILLs the daemon (no graceful shutdown — the port file is
left stale), then starts a fresh daemon on the same data directory and
asserts the session list comes back with the same session, marked
``interrupted``.

This is the manual criterion "daemon crash is detected … recovered by
restart with the session list intact", checked at the daemon+protocol level
where a test can observe it without a real UI.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from websockets.asyncio.client import connect

from tstd.protocol import PROTOCOL_VERSION
from tstd.ws import create_port_file_path


def _tstd_bin() -> str:
    return str(Path(sys.executable).parent / "tstd")


def _spawn(data_dir: Path) -> subprocess.Popen:
    """Start a real tstd daemon on the given data dir, watched by this process."""
    return subprocess.Popen(
        [
            _tstd_bin(),
            "--data-dir",
            str(data_dir),
            "--log-level",
            "DEBUG",
            "--parent-pid",
            str(os.getpid()),
        ],
        stdout=open(data_dir / "daemon.log", "w"),
        stderr=subprocess.STDOUT,
    )


def _read_port_file(path: Path) -> dict | None:
    """Synchronously read the port file, returning None if it is absent."""
    if not path.exists():
        return None
    return json.loads(path.read_text())


async def _wait_for_port_file(path: Path, pid: int, seconds: float = 10.0) -> dict:
    """Wait for the port file the daemon with the given pid wrote.

    Keying on the pid matters on restart: after a SIGKILL the previous
    daemon's port file lingers (no clean removal), so mere existence does not
    mean the fresh daemon is up. The read goes through a thread because the
    event loop must not do blocking filesystem work.
    """
    try:
        async with asyncio.timeout(seconds):
            while True:
                info = await asyncio.to_thread(_read_port_file, path)
                if info and info.get("pid") == pid:
                    return info
                await asyncio.sleep(0.05)
    except TimeoutError as e:
        raise RuntimeError(f"port file for pid {pid} never appeared at {path}") from e


async def _connect(info: dict):
    ws = await connect(f"ws://127.0.0.1:{info['port']}")
    await ws.send(
        json.dumps({"type": "hello", "token": info["token"], "version": PROTOCOL_VERSION})
    )
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _open_workspace(ws, path: str) -> str:
    await ws.send(json.dumps({"type": "open_workspace", "path": path}))
    evt = json.loads(await ws.recv())
    assert evt["type"] == "session_state"
    return evt["session_id"]


async def _list_sessions(ws) -> list[dict]:
    await ws.send(json.dumps({"type": "list_sessions"}))
    evt = json.loads(await ws.recv())
    assert evt["type"] == "session_list"
    return evt["sessions"]


class TestDaemonRestartIntegration:
    async def test_session_list_intact_after_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            sessions_file = data_dir / "sessions.json"

            # 1. Start a daemon, open a session, note its id.
            daemon = _spawn(data_dir)
            try:
                info = await _wait_for_port_file(create_port_file_path(data_dir), daemon.pid)
                ws = await _connect(info)
                session_id = await _open_workspace(ws, str(data_dir / "workspace"))
                await ws.close()
                assert sessions_file.exists(), "session should be persisted before crash"
            finally:
                daemon.kill()
                daemon.wait(timeout=5)

            # 2. Restart on the same data dir. The stale port file (with the
            #    dead daemon's pid) must be replaced.
            daemon2 = _spawn(data_dir)
            try:
                info2 = await _wait_for_port_file(create_port_file_path(data_dir), daemon2.pid)
                ws2 = await _connect(info2)
                sessions = await _list_sessions(ws2)
                await ws2.close()
            finally:
                daemon2.terminate()
                daemon2.wait(timeout=5)

            # 3. The session survived the crash as an interrupted tombstone.
            assert len(sessions) == 1
            assert sessions[0]["session_id"] == session_id
            assert sessions[0]["state"] == "interrupted"
            assert sessions[0]["workspace_path"] == str(data_dir / "workspace")

            # Clean shutdown (second daemon via SIGTERM) removes the port file.
            assert not create_port_file_path(data_dir).exists()
