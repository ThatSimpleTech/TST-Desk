"""Waiting for the UI to catch up.

Driving a desktop is a sequence, and sequences need to wait: an application takes
a second to appear, a menu takes a moment to render, a click takes time to have
an effect. Without this, the only way to wait was to shell out to ``sleep``,
which means a computer-use server could not complete a task without a second tool
beside it.

:func:`wait_for_window` is the more useful of the two, and the one that answers
the question actually being asked — "is the thing I launched ready yet?" — rather
than guessing a duration and hoping.

Both are bounded. An unbounded wait in a server that handles one request at a
time is a wedged session, and the caller cannot cancel it.
"""

from __future__ import annotations

import time
from typing import Any

from tst_cu_mcp.focus import WindowInfo, foreground_window, window_matches

#: Upper bound on a single wait. Generous enough for a cold application start,
#: short enough that a bad argument costs seconds rather than the session.
MAX_WAIT_SECONDS = 30.0

#: How often :func:`wait_for_window` re-checks. Cheap call, and a quarter second
#: is well below the threshold where a human would notice the lag.
POLL_INTERVAL_SECONDS = 0.25


def wait(seconds: float) -> dict[str, Any]:
    """Sleep for *seconds*, capped at :data:`MAX_WAIT_SECONDS`.

    Blocks the server while it runs. That is acceptable here and nowhere else:
    the stdio transport serves one client processing one call at a time, so there
    is no concurrent request to starve.
    """
    if seconds < 0:
        raise ValueError("seconds must be >= 0")
    if seconds > MAX_WAIT_SECONDS:
        raise ValueError(
            f"seconds must be <= {MAX_WAIT_SECONDS} (asked for {seconds}); "
            "call wait repeatedly, or use wait_for_window to wait on a condition "
            "rather than a duration"
        )
    time.sleep(seconds)
    return {"waited_seconds": round(seconds, 3)}


def wait_for_window(
    title: str,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Poll until the foreground window matches *title*, or *timeout_seconds* passes.

    Matching is :func:`~tst_cu_mcp.focus.window_matches` — case-insensitive
    substring against the window title or its process name.

    Returns rather than raises on timeout, reporting ``matched: false`` and what
    is actually in front. A timeout is information the caller needs to act on
    (the app failed to start, or something else stole focus), not an exception
    that discards it.

    Checks once before sleeping at all, so an already-correct foreground window
    costs nothing.
    """
    if not title.strip():
        raise ValueError("title must be non-empty")
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must be >= 0")
    if timeout_seconds > MAX_WAIT_SECONDS:
        raise ValueError(f"timeout_seconds must be <= {MAX_WAIT_SECONDS}")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be > 0")

    started = time.monotonic()
    window: WindowInfo = foreground_window()
    while True:
        if window_matches(title, window):
            return _result(True, window, started, title)
        if time.monotonic() - started >= timeout_seconds:
            return _result(False, window, started, title)
        time.sleep(min(poll_interval_seconds, timeout_seconds))
        window = foreground_window()


def _result(matched: bool, window: WindowInfo, started: float, title: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "matched": matched,
        "waited_seconds": round(time.monotonic() - started, 3),
        "foreground_window": window.to_dict(),
    }
    if not matched:
        payload["expected"] = title
        payload["hint"] = (
            f"nothing matching {title!r} came to the front. It may have failed to "
            "start, opened behind another window, or another application took focus."
        )
    return payload
