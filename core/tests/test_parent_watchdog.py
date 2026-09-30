"""Parent-death watchdog and orphan-prevention tests (TD-1002)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tstd.coworker import save_coworker
from tstd.daemon import Daemon, _parent_alive
from tstd.ws import create_port_file_path


class TestParentAlive:
    def test_live_pid_is_alive(self) -> None:
        assert _parent_alive(os.getpid())

    def test_dead_pid_is_not_alive(self) -> None:
        # Let a process exit, then its pid must read as dead.
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=10)
        assert proc.returncode == 0
        assert not _parent_alive(proc.pid)


async def _wait_for_port_file(path: Path) -> None:
    """Same readiness poll as the shutdown-message tests."""
    try:
        async with asyncio.timeout(5):
            while True:
                if await asyncio.to_thread(path.is_file):
                    return
                await asyncio.sleep(0.01)
    except TimeoutError as exc:
        raise AssertionError(f"port file never appeared at {path}") from exc


class TestWatchdog:
    async def test_daemon_shuts_down_when_parent_dies(self) -> None:
        await _parent_death(coworker=False, foreign_pid=None)

    async def test_parent_death_leaves_a_foreign_port_file(self) -> None:
        await _parent_death(coworker=False, foreign_pid=os.getpid() + 1)

    async def test_watchdog_inactive_without_parent_pid(self) -> None:
        # No --parent-pid: the daemon must not die on its own.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            daemon = Daemon(data_dir=data_dir)
            runner = asyncio.create_task(daemon.run())
            port = create_port_file_path(data_dir)
            await _wait_for_port_file(port)
            assert not daemon._shutdown_event.is_set()
            daemon._shutdown_event.set()
            await asyncio.wait_for(runner, timeout=5.0)
            assert not port.exists()

    async def test_coworker_on_still_trips_watchdog_when_parent_dies(self) -> None:
        # Close keeps the host alive, so --parent-pid stays armed even
        # when coworker.yaml is on. Fake parent death must still shut
        # tstd down (TD-2903: no orphan after SIGKILL of the host).
        await _parent_death(coworker=True, foreign_pid=None)


async def _parent_death(*, coworker: bool, foreign_pid: int | None) -> None:
    sleeper = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)"
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            if coworker:
                save_coworker(tmp, True)
            daemon = Daemon(data_dir=data_dir, parent_pid=sleeper.pid)
            # Aggressive poll so the test stays fast.
            daemon._parent_poll_interval = 0.05
            runner = asyncio.create_task(daemon.run())
            port = create_port_file_path(data_dir)
            try:
                await _wait_for_port_file(port)
                if foreign_pid is not None:
                    body = json.loads(port.read_text(encoding="utf-8"))
                    body["pid"] = foreign_pid
                    port.write_text(json.dumps(body), encoding="utf-8")
                try:
                    sleeper.kill()
                except PermissionError:
                    # macOS intermittently vetoes same-uid kills with EPERM
                    # (TD-605): the scenario cannot run this round — the
                    # product code under test was never exercised, so skip
                    # rather than fail.
                    pytest.skip("the OS vetoed the test's own kill (TD-605)")
                # Reap the child; an unreaped child lingers as a zombie that
                # os.kill(pid, 0) still reports as alive.
                await asyncio.wait_for(sleeper.wait(), timeout=5)
                await asyncio.wait_for(runner, timeout=5.0)
                assert daemon._shutdown_event.is_set()
                if foreign_pid is None:
                    assert not port.exists()
                else:
                    kept = json.loads(port.read_text(encoding="utf-8"))
                    assert kept["pid"] == foreign_pid
            finally:
                if not runner.done():
                    daemon._shutdown_event.set()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(runner, timeout=5)
    finally:
        if sleeper.returncode is None:
            with contextlib.suppress(PermissionError):
                sleeper.kill()
