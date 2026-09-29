"""POSIX shutdown signals and a hard bound on cleanup (TD-4842).

The event loop receives SIGTERM and SIGINT and sets the daemon's shutdown
event — the same path as the websocket ``shutdown`` message. Cleanup that
does not return must not leave the process sleeping: a wall-clock timer,
not the loop, ends it. ``call_later`` would never run if the loop thread
itself were the thing that stuck.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .logging import LOG_FILE_NAME, log_directory, redact_secrets
from .ws import remove_port_file

# Long enough for a quiet daemon to close its sockets and its audit
# writer. Short enough that a supervising ``kill`` is not still waiting
# when the operator reaches for SIGKILL.
SHUTDOWN_BUDGET_SECONDS = 5.0

_POSIX_SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def install_posix_shutdown_signals(callback: Callable[[], None]) -> None:
    """Point SIGTERM and SIGINT at *callback* on the running loop.

    Windows has no ``add_signal_handler``. Shutdown there stays the
    websocket message, the parent watchdog, or the console event the
    host already sends.
    """
    if sys.platform == "win32":
        return
    loop = asyncio.get_running_loop()
    for sig in _POSIX_SHUTDOWN_SIGNALS:
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, callback)


def remove_posix_shutdown_signals() -> None:
    """Drop the handlers :func:`install_posix_shutdown_signals` installed.

    An in-process daemon must not leave its callback on the caller's
    loop after ``run`` returns.
    """
    if sys.platform == "win32":
        return
    loop = asyncio.get_running_loop()
    for sig in _POSIX_SHUTDOWN_SIGNALS:
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.remove_signal_handler(sig)


class ShutdownBudget:
    """Exit the process if signaled cleanup outlives the budget.

    ``exit_process`` defaults to ``os._exit``. A normal ``SystemExit``
    still runs ``asyncio.run``'s executor shutdown, which waits out a
    thread the cleanup is stuck in — the hang the budget exists to end.
    """

    def __init__(
        self,
        seconds: float = SHUTDOWN_BUDGET_SECONDS,
        *,
        exit_process: Callable[[int], None] | None = None,
    ) -> None:
        self._seconds = seconds
        self._exit = os._exit if exit_process is None else exit_process
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._data_dir: Path | None = None
        self._finished = False
        self._expired = False

    @property
    def pending(self) -> bool:
        """True after :meth:`arm` until :meth:`finish` or expiry."""
        with self._lock:
            return self._timer is not None and not self._finished and not self._expired

    def arm(self, data_dir: Path) -> None:
        """Start the budget once. A second signal does not extend it."""
        with self._lock:
            if self._timer is not None or self._finished or self._expired:
                return
            self._data_dir = data_dir
            timer = threading.Timer(self._seconds, self._expire)
            timer.daemon = True
            self._timer = timer
        timer.start()

    def finish(self) -> None:
        """Cleanup returned. The timer must not exit the process."""
        with self._lock:
            if self._expired:
                return
            self._finished = True
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()

    def _expire(self) -> None:
        with self._lock:
            if self._finished or self._expired:
                return
            self._expired = True
            data_dir = self._data_dir
        try:
            if data_dir is not None:
                with contextlib.suppress(OSError):
                    remove_port_file(data_dir)
                _note_timeout(data_dir, self._seconds)
        finally:
            self._exit(1)


def _note_timeout(data_dir: Path, seconds: float) -> None:
    """One JSON line, without the logging lock.

    The loop thread may be inside ``emit`` holding that lock. Waiting
    on it would burn the budget that is supposed to end the process.
    The sentence is fixed. It is still passed through redaction so a
    later edit cannot drop a secret into this path by accident.
    """
    message = redact_secrets(f"shutdown exceeded the {seconds:.0f}s budget")
    directory = log_directory(data_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": f"{time.time():.3f}",
                "level": "ERROR",
                "logger": "tstd.shutdown",
                "message": message,
            },
            ensure_ascii=False,
        )
        with (directory / LOG_FILE_NAME).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
    except OSError:
        return
