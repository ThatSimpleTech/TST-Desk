"""Windows console close, logoff, and shutdown (TD-4848).

Those events do not become signals. Python never installs a handler
for them, and Windows can end the process when the handler returns,
so the port file is released on this thread before the loop is asked
to shut down. Ctrl+C and Ctrl+Break stay on ``signal.signal`` — the
same callback SIGTERM uses, which arms the shutdown budget.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any, cast

from .logging import get_logger
from .port_file import release_port_file

log = get_logger("tstd.console")

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6

# Windows ends the process after these return. The file has to be gone
# before that, on this thread, not on the loop.
_RELEASE_EVENTS = frozenset({CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT})

_HANDLER: Any = None
_PREVIOUS: dict[int, Any] = {}
_ARMED = False


def on_console_close(event: int, data_dir: Path, request: Callable[[], None]) -> bool:
    """Release the port file and ask the daemon to stop.

    Returns True when *event* is close, logoff, or shutdown. Ctrl+C,
    Ctrl+Break, and anything else return False and do not call
    *request*, so the signal handler remains the owner of those.
    """
    if event not in _RELEASE_EVENTS:
        return False
    release_port_file(data_dir)
    request()
    return True


def install_windows_console_shutdown(callback: Callable[[], None], data_dir: Path) -> None:
    """Install the console hook and the Ctrl+C / Ctrl+Break handlers.

    No-op off Windows, and a second call does not stack handlers.
    *callback* is the daemon's signal callback. It is scheduled onto
    the running loop: ``Event.set`` from the console thread is not
    the contract, and the handler may return into process teardown.
    """
    global _ARMED, _HANDLER
    if sys.platform != "win32" or _ARMED:
        return
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return
    # Set before the signal swap so a failed console hook cannot stack
    # a second SIGINT handler over the one just installed.
    _ARMED = True
    loop = asyncio.get_running_loop()

    def request() -> None:
        try:
            loop.call_soon_threadsafe(callback)
        except RuntimeError:
            # The loop is already closed. The file was released by the
            # caller before this schedule.
            return

    _install_signals(request)
    handler = _console_handler(data_dir, request)
    # WINFUNCTYPE exists only on Windows. Off Windows this function is
    # not called; a missing factory means the close hook cannot be armed.
    if handler is None or not windll.kernel32.SetConsoleCtrlHandler(handler, True):
        log.warning("console close handler was not installed")
        return
    # Keep the ctypes object alive. Windows calls the raw pointer; if
    # this object is collected, the next console event jumps nowhere.
    _HANDLER = handler


def remove_windows_console_shutdown() -> None:
    """Drop the handlers :func:`install_windows_console_shutdown` installed.

    Safe when install was a no-op. An in-process daemon must not leave
    its callback on the caller's signals after ``run`` returns.
    """
    global _ARMED, _HANDLER
    if sys.platform != "win32":
        return
    _ARMED = False
    handler = _HANDLER
    _HANDLER = None
    if handler is not None:
        windll = getattr(ctypes, "windll", None)
        if windll is not None:
            with contextlib.suppress(OSError):
                windll.kernel32.SetConsoleCtrlHandler(handler, False)
    previous = list(_PREVIOUS.items())
    _PREVIOUS.clear()
    for sig, old in previous:
        with contextlib.suppress(OSError, ValueError):
            signal.signal(sig, old)


def _install_signals(request: Callable[[], None]) -> None:
    """Ctrl+C and Ctrl+Break via ``signal.signal``.

    ``add_signal_handler`` is not implemented on Windows. SIGBREAK
    exists only there; other platforms never reach this function.
    """

    def _handler(_signum: int, _frame: FrameType | None) -> None:
        request()

    for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", None)):
        if not isinstance(sig, int):
            continue
        _PREVIOUS[sig] = signal.signal(sig, _handler)


def _console_handler(data_dir: Path, request: Callable[[], None]) -> Any:
    def _body(event: int) -> int:
        if on_console_close(int(event), data_dir, request):
            return 1
        return 0

    # Direct attribute access fails mypy off Windows: the stdlib stub
    # only defines WINFUNCTYPE on win32.
    factory = getattr(cast(Any, ctypes), "WINFUNCTYPE", None)
    if factory is None:
        return None
    return factory(ctypes.c_int, ctypes.c_uint)(_body)
