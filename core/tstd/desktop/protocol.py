"""Desktop computer-use driver contract (TD-3301).

Path and host rules do not apply to these tools. The focus guard
(``expect_window``) and the process-wide kill-switch do. Capture must
never move a pointer.
"""

from __future__ import annotations

import base64
from typing import Protocol

# 1x1 PNG so CI can exercise screenshot without a display.
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwAD"
    "hgGAWjR9awAAAABJRU5ErkJggg=="
)
TINY_PNG = base64.b64decode(TINY_PNG_B64)


class DesktopError(Exception):
    """Typed refusal from a desktop driver: no actuation after this.

    Lives here, not under ``tstd.tools``, so importing the driver cannot
    cycle through ``tools/__init__`` → handlers → desktop.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def window_matches(expected: str, title: str, app: str) -> bool:
    """Case-insensitive substring against title or process name.

    Same policy as ``tst_cu_mcp.focus.window_matches``: a volatile browser
    title still matches ``chrome``. Empty *expected* matches nothing —
    that is almost always an unset variable, not a request to skip the
    guard.
    """
    needle = expected.strip().casefold()
    if not needle:
        return False
    return needle in title.casefold() or needle in app.casefold()


class DesktopDriver(Protocol):
    """Capture and actuation. Implementations must not bind a socket."""

    def set_killed(self, killed: bool) -> None:
        """Engage or clear the kill-switch. Screenshot still runs."""

    async def screenshot(self, display: int | None = None) -> str:
        """Return a JSON object with ``png_base64`` (and size if known)."""

    async def move(self, x: float, y: float, expect_window: str | None = None) -> str:
        """Move the pointer. Refuses on kill-switch or focus mismatch."""

    async def click(
        self,
        x: float,
        y: float,
        button: str = "left",
        count: int = 1,
        expect_window: str | None = None,
    ) -> str:
        """Click at a point."""

    async def type_text(self, text: str, expect_window: str | None = None) -> str:
        """Type at the current focus. The text itself is not returned."""

    async def scroll(
        self,
        dx: int = 0,
        dy: int = 0,
        x: float | None = None,
        y: float | None = None,
        expect_window: str | None = None,
    ) -> str:
        """Scroll; optional move to *x*, *y* first."""

    async def aclose(self) -> None:
        """Reap a sidecar if this driver owns one."""
