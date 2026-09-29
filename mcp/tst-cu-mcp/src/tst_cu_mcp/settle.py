"""Wait inside an action so the model skips a round-trip (TD-4847).

A live session spent almost all of its wall clock on time-to-first-token,
so a separate ``wait`` plus ``get_foreground_window`` costs more than the
desktop work. The short pause belongs on the call that needed it. Longer
waits stay on ``wait`` / ``wait_for_window`` — this server handles one
stdio call at a time, and a multi-second cap here is the wedge bound.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field

from tst_cu_mcp.focus import WindowInfo, foreground_window

#: Inclusive. Five seconds covers a menu or a window finishing focus.
#: Anything longer is a condition to poll, not a nap bolted to a click.
MAX_MS = 5000

SETTLE_NOTE = (
    " Optional `settle_ms` (integer milliseconds, 0-5000) waits after the "
    "action, then this result includes `foreground_window` (`app` and "
    "`title`) so you can skip a separate wait and get_foreground_window. "
    "Omit it or pass 0 to act immediately."
)

AFTER_NOTE = (
    " Optional `after_ms` (integer milliseconds, 0-5000) waits before the "
    "capture so wait-then-look is one call. Omit it or pass 0 to capture "
    "immediately."
)

_FIELD_DESC = f"Integer milliseconds from 0 to {MAX_MS} inclusive."

SettleMs = Annotated[int, Field(ge=0, le=MAX_MS, description=_FIELD_DESC)]
AfterMs = Annotated[int, Field(ge=0, le=MAX_MS, description=_FIELD_DESC)]

log = logging.getLogger("tst_cu_mcp.settle")

Sleep = Callable[[float], None]
Reader = Callable[[], WindowInfo]


def bound_ms(value: object, *, name: str) -> int:
    """Reject anything outside 0..MAX_MS before the desktop is touched.

    ``bool`` is an ``int`` subclass and must not pass: ``True`` would
    become a 1 ms wait. Callers check this before actuating so a bad
    wait cannot click, type, or launch.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer number of milliseconds")
    if value < 0 or value > MAX_MS:
        raise ValueError(f"{name} must be from 0 to {MAX_MS} milliseconds (got {value})")
    return value


def pause(ms: int, *, sleep: Sleep | None = None) -> None:
    """Sleep *ms* milliseconds. Zero does not call *sleep*.

    The default sleeper is :func:`time.sleep`. Tests replace that (or
    pass *sleep*) so a wait is observable without delaying the suite.
    """
    if ms <= 0:
        return
    (sleep or time.sleep)(ms / 1000.0)


def _identity(read: Reader) -> dict[str, str]:
    try:
        window = read()
    except Exception as exc:
        # The action already happened. Failing the tool invites a second
        # click. The exception text can name a window or a path, so the
        # result only carries a stable code.
        log.warning("foreground window unread after action (%s)", type(exc).__name__)
        return {"app": "", "title": "", "read": "unavailable"}
    return {"app": window.process, "title": window.title}


def with_foreground(
    payload: dict[str, Any],
    settle_ms: int,
    *,
    sleep: Sleep | None = None,
    read: Reader | None = None,
) -> dict[str, Any]:
    """Wait *settle_ms*, then attach the foreground app and title.

    *settle_ms* is validated again here. Callers still validate before
    actuating; this second check is what a direct caller cannot skip.
    """
    ms = bound_ms(settle_ms, name="settle_ms")
    pause(ms, sleep=sleep)
    out = dict(payload)
    out["foreground_window"] = _identity(read or foreground_window)
    return out
