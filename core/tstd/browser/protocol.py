"""Browser computer-use driver contract (TD-1710).

Six verbs: navigate, click, type, scroll, screenshot, wait. Path and host
rules do not apply. Capture must never actuate. Implementations must not
bind a socket.
"""

from __future__ import annotations

import base64
import struct
from typing import Any, Protocol

# 1x1 PNG so CI can exercise screenshot without launching Chrome.
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwAD"
    "hgGAWjR9awAAAABJRU5ErkJggg=="
)
TINY_PNG = base64.b64decode(TINY_PNG_B64)


class BrowserError(Exception):
    """Typed refusal from a browser driver: no actuation after this.

    Lives here, not under ``tstd.tools``, so importing the driver cannot
    cycle through ``tools/__init__`` → handlers → browser.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def png_size(data: bytes) -> tuple[int, int]:
    """Read width and height from a PNG IHDR. ``(0, 0)`` if not a PNG."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    width, height = struct.unpack(">II", data[16:24])
    return (int(width), int(height))


class BrowserDriver(Protocol):
    """In-process browser surface. Implementations must not bind a socket."""

    async def navigate(self, url: str) -> dict[str, Any]:
        """Open *url* and return ``url`` / ``title``."""

    async def click(self, x: float, y: float, button: str = "left") -> dict[str, Any]:
        """Click at a page point."""

    async def type_text(self, text: str) -> dict[str, Any]:
        """Type at the current focus. The text itself is not returned."""

    async def scroll(self, dx: int = 0, dy: int = 0) -> dict[str, Any]:
        """Scroll the page."""

    async def screenshot_png(self) -> bytes:
        """Capture the current page as PNG bytes. Never actuates."""

    async def wait(self, timeout_ms: int = 1000, selector: str | None = None) -> dict[str, Any]:
        """Wait for a timeout and/or a selector."""

    async def aclose(self) -> None:
        """Release a live browser if this driver owns one."""
