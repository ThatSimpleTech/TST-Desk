"""Browser computer-use driver contract (TD-1710).

Six verbs: navigate, click, type, scroll, screenshot, wait. Path and host
rules do not apply. Capture must never actuate. Implementations must not
bind a socket.
"""

from __future__ import annotations

import base64
from typing import Any, Protocol

from ..screen.frames import png_size as png_size

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

    async def hit_test(self, x: float, y: float) -> dict[str, Any]:
        """Observe the node at a CSS-pixel point. Never actuates."""

    async def aclose(self) -> None:
        """Release a live browser if this driver owns one."""


def scripted_hit_node(x: float, y: float) -> dict[str, Any]:
    """CI / mock node for Design mode. Capture never actuates."""
    ix, iy = int(x), int(y)
    return {
        "xpath": f"//*[@data-mock-point='{ix},{iy}']",
        "role": "button",
        "attributes": {"id": "mock-target", "data-x": str(ix), "data-y": str(iy)},
        "box": {"x": x - 20.0, "y": y - 10.0, "width": 80.0, "height": 24.0},
        "styles": {"display": "inline-block", "font-size": "14px"},
    }


def _as_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_str_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(key, str) and item is not None:
            out[key] = str(item)
    return out


def normalize_hit(raw: object, x: float, y: float) -> dict[str, Any]:
    """Coerce a driver hit into xpath / role / attributes / box / styles."""
    data: dict[str, Any] = raw if isinstance(raw, dict) else {}
    box_raw = data.get("box")
    if isinstance(box_raw, dict):
        box = {
            "x": _as_float(box_raw.get("x"), x),
            "y": _as_float(box_raw.get("y"), y),
            "width": _as_float(box_raw.get("width"), 0.0),
            "height": _as_float(box_raw.get("height"), 0.0),
        }
    else:
        box = {"x": x, "y": y, "width": 0.0, "height": 0.0}
    xpath = data.get("xpath")
    role = data.get("role")
    return {
        "xpath": xpath if isinstance(xpath, str) else None,
        "role": role if isinstance(role, str) else None,
        "attributes": _as_str_map(data.get("attributes")),
        "box": box,
        "styles": _as_str_map(data.get("styles")),
    }
