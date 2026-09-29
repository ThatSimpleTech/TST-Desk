"""Wait inside a desktop tool so the model skips a round-trip (TD-4847).

The sidecar tools implement the same wait for a model that calls them
directly. ``desktop_*`` handlers wait here and then read the driver.
They do not also pass ``settle_ms`` / ``after_ms`` to the sidecar, which
would pause twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any, Protocol

#: Inclusive. Matches ``tst_cu_mcp.settle.MAX_MS``. Five seconds is enough
#: for a menu to finish drawing; a longer wait stays a separate ``wait``
#: call so one tool invocation cannot wedge the turn.
MAX_MS = 5000

_SETTLE_DESC = (
    "Milliseconds to wait after the action (0-5000) before this result's "
    "foreground_window (app and title) is read. Omit or 0 to act immediately."
)
_AFTER_DESC = (
    "Milliseconds to wait before capturing (0-5000). Omit or 0 to capture "
    "immediately, so wait-then-look is one call."
)

log = logging.getLogger("tstd.desktop.settle")


def _ms_schema(description: str) -> dict[str, Any]:
    return {
        "type": "integer",
        "minimum": 0,
        "maximum": MAX_MS,
        "default": 0,
        "description": description,
    }


SETTLE_MS_SCHEMA = _ms_schema(_SETTLE_DESC)
AFTER_MS_SCHEMA = _ms_schema(_AFTER_DESC)


class ForegroundSource(Protocol):
    """A driver that can name the window in front without actuating."""

    async def foreground_window(self) -> Mapping[str, Any]:
        """Return at least ``app`` or ``process``, and ``title``."""


def bound_ms(value: object, *, name: str) -> int:
    """``None`` is the omitted argument: no wait. Anything else is 0..MAX_MS.

    ``bool`` is rejected. It is an ``int`` subclass, and ``True`` must not
    become a 1 ms pause. Call this before actuating.
    """
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer number of milliseconds")
    if value < 0 or value > MAX_MS:
        raise ValueError(f"{name} must be from 0 to {MAX_MS} milliseconds (got {value})")
    return value


async def pause(ms: int) -> None:
    """Sleep *ms* milliseconds. Zero does not call ``asyncio.sleep``.

    Tests replace ``asyncio.sleep`` on this module. That is the clock.
    """
    if ms <= 0:
        return
    await asyncio.sleep(ms / 1000.0)


def window_identity(window: Mapping[str, Any]) -> dict[str, str]:
    """The two fields a follow-up ``get_foreground_window`` was for.

    ``app`` is the process name. Bounds and pid stay on the dedicated
    read; this result only exists to save the round-trip.
    """
    app = window.get("app")
    if not isinstance(app, str):
        process = window.get("process")
        app = process if isinstance(process, str) else ""
    title = window.get("title")
    return {"app": app, "title": title if isinstance(title, str) else ""}


def _as_object(raw: str) -> dict[str, Any]:
    try:
        loaded: object = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    if isinstance(loaded, dict):
        return loaded
    return {"raw": raw}


async def with_foreground(raw: str, driver: ForegroundSource, settle_ms: int) -> str:
    """Wait, then attach ``foreground_window`` to a driver JSON result.

    A failed read does not fail the action. The click or key already
    landed; an error result would make the model do it again. The
    exception text is not copied — it can name a window or a path.
    """
    await pause(settle_ms)
    try:
        window = await driver.foreground_window()
    except Exception as exc:
        log.warning("foreground window unread after action (%s)", type(exc).__name__)
        identity: dict[str, str] = {"app": "", "title": "", "read": "unavailable"}
    else:
        identity = window_identity(window)
    payload = _as_object(raw)
    payload["foreground_window"] = identity
    return json.dumps(payload)
