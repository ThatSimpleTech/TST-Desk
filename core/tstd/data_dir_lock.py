"""One daemon per data dir (TD-5022).

Two daemons on one data dir race on ``port.json``, the session store and
every saved conversation. A restart that overlaps the old process (a
slow shutdown, a second launch, ``tst`` starting one while the host's is
still coming up) is how two happen. ``main()`` takes an OS file lock on
``{data_dir}/tstd.lock`` before it builds the daemon and holds it for the
life of the process: ``flock`` on POSIX, a one-byte ``msvcrt`` lock on
Windows (the autonomy ledger's pattern). The OS drops it when the process
ends, ``kill -9`` included, so a crash never leaves a stale lock. The file
itself is never deleted: unlinking a lock file another process is waiting
on lets a third take a lock on a new inode.

Tests that build ``Daemon`` directly never take it.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .logging import get_logger

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

log = get_logger("tstd.data_dir_lock")

LOCK_FILE_NAME = "tstd.lock"
# Longer than the shutdown budget (5 s), so an old daemon that is still
# closing hands over instead of making the new one fail.
LOCK_WAIT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.2


def lock_path(data_dir: Path) -> Path:
    return data_dir / LOCK_FILE_NAME


class DataDirLock:
    """The data dir's lock, held by this process or not at all."""

    def __init__(self, data_dir: Path) -> None:
        self.path = lock_path(data_dir)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Take the lock when it is free. Never blocks."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # os.open fds are not inherited (PEP 446): a shell tool or MCP
        # server this daemon starts never keeps the lock after it exits.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if sys.platform == "win32":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        """Drop the lock. Exiting does the same."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if sys.platform == "win32":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)


def acquire_data_dir_lock(
    data_dir: Path,
    *,
    wait_s: float = LOCK_WAIT_SECONDS,
    poll_s: float = LOCK_POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> DataDirLock | None:
    """The held lock, or None when another process kept it for *wait_s*.

    Logs "waiting for previous daemon" once when the first try fails.
    """
    lock = DataDirLock(data_dir)
    if lock.try_acquire():
        return lock
    log.info("waiting for previous daemon", extra={"extra_fields": {"wait_s": wait_s}})
    deadline = clock() + wait_s
    while clock() < deadline:
        sleep(poll_s)
        if lock.try_acquire():
            return lock
    return None
