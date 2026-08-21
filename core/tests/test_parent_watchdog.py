"""Parent-death watchdog and orphan-prevention tests (TD-1002)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tstd.coworker import save_coworker
from tstd.daemon import Daemon, _parent_alive


class TestParentAlive:
    def test_live_pid_is_alive(self) -> None:
        assert _parent_alive(os.getpid())

    def test_dead_pid_is_not_alive(self) -> None:
        # Let a process exit, then its pid must read as dead.
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=10)
        assert proc.returncode == 0
        assert not _parent_alive(proc.pid)


class TestWatchdog:
    async def test_daemon_shuts_down_when_parent_dies(self) -> None:
        sleeper = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)"
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                daemon = Daemon(data_dir=Path(tmp), parent_pid=sleeper.pid)
                # Aggressive poll so the test stays fast.
                daemon._parent_poll_interval = 0.05
                runner = asyncio.create_task(daemon.run())
                await asyncio.sleep(0.2)  # let the watchdog start
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
        finally:
            if sleeper.returncode is None:
                with contextlib.suppress(PermissionError):
                    sleeper.kill()

    async def test_watchdog_inactive_without_parent_pid(self) -> None:
        # No --parent-pid: the daemon must not die on its own.
        with tempfile.TemporaryDirectory() as tmp:
            daemon = Daemon(data_dir=Path(tmp))
            runner = asyncio.create_task(daemon.run())
            await asyncio.sleep(0.2)
            assert not daemon._shutdown_event.is_set()
            assert runner is not None
            # Now shut it down via the event so the test doesn't leak a task.
            daemon._shutdown_event.set()
            await asyncio.wait_for(runner, timeout=5.0)

    async def test_coworker_on_survives_fake_parent_death(self) -> None:
        # Host omits --parent-pid when coworker.yaml is on. A dying
        # window process must not take tstd with it.
        sleeper = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)"
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                save_coworker(tmp, True)
                daemon = Daemon(data_dir=Path(tmp))
                daemon._parent_poll_interval = 0.05
                runner = asyncio.create_task(daemon.run())
                await asyncio.sleep(0.2)
                try:
                    sleeper.kill()
                except PermissionError:
                    pytest.skip("the OS vetoed the test's own kill (TD-605)")
                await asyncio.wait_for(sleeper.wait(), timeout=5)
                await asyncio.sleep(0.3)
                assert not daemon._shutdown_event.is_set()
                assert not runner.done()
                daemon._shutdown_event.set()
                await asyncio.wait_for(runner, timeout=5.0)
        finally:
            if sleeper.returncode is None:
                with contextlib.suppress(PermissionError):
                    sleeper.kill()
