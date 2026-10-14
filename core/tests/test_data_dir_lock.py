"""One daemon per data dir (TD-5022).

The unit tests take the lock in this process. The others run real
``python -m tstd.daemon`` processes on one data dir: a second waits for
the first, starts when the first exits, and gives up when it does not.
Readiness is the daemon's own stdout lines, never a sleep.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.test_daemon_shutdown import _read_log
from tstd.cli import daemon_argv
from tstd.data_dir_lock import (
    LOCK_WAIT_SECONDS,
    DataDirLock,
    acquire_data_dir_lock,
    lock_path,
)
from tstd.ws import read_port_file

_CORE = Path(__file__).resolve().parent.parent


# ── the lock itself ──────────────────────────────────────────────────


class TestDataDirLock:
    def test_second_holder_is_refused_until_the_first_releases(self, tmp_path: Path) -> None:
        first = DataDirLock(tmp_path)
        second = DataDirLock(tmp_path)
        try:
            assert first.try_acquire()
            assert first.held
            assert not second.try_acquire()
            assert not second.held
            first.release()
            assert second.try_acquire()
        finally:
            first.release()
            second.release()
        # The file stays: unlinking it would let a third take a new inode.
        assert lock_path(tmp_path).is_file()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_lock_file_is_private(self, tmp_path: Path) -> None:
        lock = DataDirLock(tmp_path / "fresh")
        try:
            assert lock.try_acquire()
            assert lock_path(tmp_path / "fresh").stat().st_mode & 0o777 == 0o600
        finally:
            lock.release()

    def test_free_lock_is_taken_without_waiting(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="tstd.data_dir_lock"):
            lock = acquire_data_dir_lock(tmp_path, sleep=_no_sleep)
        try:
            assert lock is not None and lock.held
            assert not caplog.records
        finally:
            assert lock is not None
            lock.release()

    def test_held_lock_waits_then_gives_up(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        holder = DataDirLock(tmp_path)
        assert holder.try_acquire()
        now = [0.0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        try:
            with caplog.at_level(logging.INFO, logger="tstd.data_dir_lock"):
                lock = acquire_data_dir_lock(
                    tmp_path, wait_s=1.0, poll_s=0.2, clock=lambda: now[0], sleep=sleep
                )
            assert lock is None
            assert 1.0 <= now[0] < 1.5
            waiting = [r for r in caplog.records if r.getMessage() == "waiting for previous daemon"]
            assert len(waiting) == 1
        finally:
            holder.release()

    def test_lock_freed_during_the_wait_is_taken(self, tmp_path: Path) -> None:
        holder = DataDirLock(tmp_path)
        assert holder.try_acquire()
        polls = [0]

        def sleep(_seconds: float) -> None:
            polls[0] += 1
            if polls[0] == 3:
                holder.release()

        lock = acquire_data_dir_lock(tmp_path, wait_s=10.0, sleep=sleep)
        try:
            assert lock is not None and lock.held
            assert polls[0] == 3
        finally:
            assert lock is not None
            lock.release()


def _no_sleep(_seconds: float) -> None:
    raise AssertionError("a free lock must not wait")


# ── real daemons ─────────────────────────────────────────────────────


class _Lines:
    """One reader for a daemon's stdout, with a wait per phrase."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self._lines: list[str] = []
        self._changed = threading.Condition()
        threading.Thread(target=self._read, name="tstd-stdout", daemon=True).start()

    def _read(self) -> None:
        assert self._proc.stdout is not None
        for raw in iter(self._proc.stdout.readline, b""):
            with self._changed:
                self._lines.append(raw.decode(errors="replace"))
                self._changed.notify_all()
        with self._changed:
            self._changed.notify_all()

    def wait_for(self, phrase: str, seconds: float = 15.0) -> None:
        with self._changed:
            ok = self._changed.wait_for(
                lambda: any(phrase in line for line in self._lines), timeout=seconds
            )
            tail = "".join(self._lines)[-2000:]
        if not ok:
            raise AssertionError(f"{phrase!r} never logged (exit={self._proc.poll()}): {tail}")


def _spawn(data_dir: Path) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        daemon_argv(data_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(_CORE),
        env=env,
    )


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        try:
            proc.kill()
        except PermissionError:
            return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return
    if proc.stdout is not None:
        proc.stdout.close()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is not installed on Windows")
def test_second_daemon_waits_then_starts_when_the_first_exits(tmp_path: Path) -> None:
    data_dir = tmp_path / "scratch"
    first = _spawn(data_dir)
    second: subprocess.Popen[bytes] | None = None
    try:
        _Lines(first).wait_for("ws server started")
        second = _spawn(data_dir)
        second_out = _Lines(second)
        second_out.wait_for("waiting for previous daemon")
        assert second.poll() is None
        info = read_port_file(data_dir)
        assert info is not None and info["pid"] == first.pid

        try:
            os.kill(first.pid, signal.SIGTERM)
        except PermissionError:
            pytest.skip("the OS vetoed the test's own kill (TD-605)")
        assert first.wait(timeout=10) == 0
        second_out.wait_for("ws server started")
        info = read_port_file(data_dir)
        assert info is not None and info["pid"] == second.pid
    finally:
        _stop(first)
        if second is not None:
            _stop(second)


def test_second_daemon_gives_up_while_the_first_runs(tmp_path: Path) -> None:
    data_dir = tmp_path / "scratch"
    first = _spawn(data_dir)
    second: subprocess.Popen[bytes] | None = None
    try:
        _Lines(first).wait_for("ws server started")
        started = time.monotonic()
        second = _spawn(data_dir)
        _Lines(second).wait_for("waiting for previous daemon")
        code = second.wait(timeout=LOCK_WAIT_SECONDS + 10)
        assert code == 1
        assert time.monotonic() - started >= LOCK_WAIT_SECONDS - 0.5
        assert "another tstd owns this data dir" in _read_log(data_dir)
        # The first never noticed.
        assert first.poll() is None
        info = read_port_file(data_dir)
        assert info is not None and info["pid"] == first.pid
    finally:
        _stop(first)
        if second is not None:
            _stop(second)
