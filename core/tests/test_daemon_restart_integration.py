"""End-to-end daemon crash + restart with the session list intact (TD-1002).

Spawns the real ``tstd`` daemon as a child process, opens a session over
the WebSocket, SIGKILLs the daemon (no graceful shutdown — the port file is
left stale), then starts a fresh daemon on the same data directory.

A session that wrote a conversation snapshot comes back ``running`` — the
same chat, same id. A session with no snapshot stays ``interrupted``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from tstd.protocol import PROTOCOL_VERSION
from tstd.ws import create_port_file_path


def _spawn(data_dir: Path) -> subprocess.Popen:
    """Start a real tstd daemon on the given data dir, watched by this process.

    Spawned as ``python -m tstd.daemon`` rather than the console script:
    uv's Windows script wrappers are trampoline exes that launch a child
    python, so the Popen pid would be the launcher's — the port file's pid
    would never match it, and ``kill()`` would orphan the real daemon with
    its data-dir files still open (TD-1406).
    """
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tstd.daemon",
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


async def _wait_for_port_file(path: Path, pid: int, seconds: float | None = None) -> dict:
    """Wait for the port file the daemon with the given pid wrote.

    Keying on the pid matters on restart: after a SIGKILL the previous
    daemon's port file lingers (no clean removal), so mere existence does not
    mean the fresh daemon is up. The read goes through a thread because the
    event loop must not do blocking filesystem work.
    """
    if seconds is None:
        seconds = 30.0 if sys.platform == "win32" else 10.0
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


async def _new_session(ws, session_id: str) -> str:
    await ws.send(json.dumps({"type": "new_session", "session_id": session_id}))
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
            scenario_ran = False
            try:
                info = await _wait_for_port_file(create_port_file_path(data_dir), daemon.pid)
                ws = await _connect(info)
                (data_dir / "workspace").mkdir()
                live_id = await _open_workspace(ws, str(data_dir / "workspace"))
                tomb_id = await _new_session(ws, live_id)
                shutil.rmtree(data_dir / "sessions" / tomb_id)
                await ws.close()
                assert sessions_file.exists(), "session should be persisted before crash"
                scenario_ran = True
            finally:
                try:
                    daemon.kill()
                except PermissionError:
                    # macOS intermittently vetoes same-uid kills with EPERM
                    # (TD-605; retries are futile for the vetoed group). With
                    # the "crash" vetoed the restart leg never exercises the
                    # product — skip rather than fail, but never mask a real
                    # failure from the body above. The leaked daemon exits via
                    # its --parent-pid watchdog when the test process dies.
                    if scenario_ran:
                        pytest.skip("the OS vetoed the test's own kill (TD-605)")
                else:
                    daemon.wait(timeout=5)

            # 2. Restart on the same data dir. The stale port file (with the
            #    dead daemon's pid) must be replaced.
            daemon2 = _spawn(data_dir)
            try:
                info2 = await _wait_for_port_file(create_port_file_path(data_dir), daemon2.pid)
                ws2 = await _connect(info2)
                sessions = await _list_sessions(ws2)
                if sys.platform == "win32":
                    # Popen.terminate is TerminateProcess on Windows — a hard
                    # kill that runs no cleanup — so the clean-shutdown leg
                    # (port file removal) goes through the protocol's
                    # shutdown message, the graceful path a host drives there.
                    await ws2.send(json.dumps({"type": "shutdown"}))
                    await ws2.close()
                    daemon2.wait(timeout=10)
                else:
                    await ws2.close()
                    try:
                        daemon2.terminate()
                    except PermissionError:
                        # The OS vetoed the scenario's own SIGTERM (TD-605): the
                        # clean-shutdown leg was never driven, so the assertions
                        # below are meaningless this round — skip rather than
                        # fail (or hang in wait until TimeoutExpired masks it).
                        pytest.skip("the OS vetoed the test's own kill (TD-605)")
                    daemon2.wait(timeout=5)
            finally:
                if daemon2.poll() is None:
                    try:
                        daemon2.kill()
                    except PermissionError:
                        # Vetoed cleanup kill (TD-605): leave the daemon to its
                        # --parent-pid watchdog rather than fail the round.
                        pass
                    else:
                        daemon2.wait(timeout=5)

            # 3. Snapshot on disk → same chat, running. No snapshot → tombstone.
            by_id = {row["session_id"]: row for row in sessions}
            assert set(by_id) == {live_id, tomb_id}
            assert by_id[live_id]["state"] == "running"
            assert by_id[tomb_id]["state"] == "interrupted"
            assert by_id[live_id]["workspace_path"] == str(data_dir / "workspace")

            # Clean shutdown (second daemon: SIGTERM on POSIX, the protocol
            # shutdown message on Windows) removes the port file.
            assert not create_port_file_path(data_dir).exists()
