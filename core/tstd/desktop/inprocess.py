"""In-process Linux computer-use driver (TD-1727).

A frozen onefile ``tstd`` cannot spawn a second copy of itself as
``tstd --cu-mcp`` — the child dies and desktop tools report Connection
lost. On Linux the capture/input backends run in this process instead.
macOS keeps the sidecar (TCC identity). Windows keeps stdio MCP.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any

from .protocol import DesktopError

_STOP_ENV = "TST_CU_MCP_STOP"


def _map_error(exc: BaseException) -> DesktopError:
    name = type(exc).__name__
    text = str(exc)
    if name in {"WaylandUnsupportedError", "WaylandCaptureError", "WaylandInputError"}:
        return DesktopError(DesktopError.WAYLAND, text)
    if name == "KillSwitchEngaged":
        return DesktopError("cu_killed", text)
    if name == "WindowFocusError" or "expected the foreground window" in text.casefold():
        return DesktopError("focus_mismatch", text)
    if name == "UnsupportedPlatformError":
        return DesktopError("e20", text)
    return DesktopError("cu_error", text or name)


class InProcessDesktopDriver:
    """Linux X11/Wayland backends, no child process."""

    def __init__(self) -> None:
        self.killed = False
        self.platform = "linux"

    def set_killed(self, killed: bool) -> None:
        self.killed = bool(killed)
        if killed:
            os.environ[_STOP_ENV] = "1"
        else:
            os.environ.pop(_STOP_ENV, None)

    async def set_overlay_session(self, active: bool) -> None:
        try:
            from tst_cu_mcp.overlay import get_overlay

            overlay = get_overlay()
            if active:
                overlay.begin_session()
            else:
                overlay.end_session()
        except Exception:
            return

    async def screenshot(self, display: int | None = None) -> str:
        from tst_cu_mcp.capture import capture

        try:
            result = await asyncio.to_thread(capture, display_index=display)
        except Exception as exc:
            raise _map_error(exc) from exc
        return json.dumps(
            {
                "png_base64": base64.b64encode(result.png_bytes).decode("ascii"),
                "width": result.image_px_width,
                "height": result.image_px_height,
            }
        )

    async def move(self, x: float, y: float, expect_window: str | None = None) -> str:
        from tst_cu_mcp.input_control import move_mouse

        try:
            await asyncio.to_thread(move_mouse, x, y, expect_window)
        except Exception as exc:
            raise _map_error(exc) from exc
        return json.dumps({"moved_to": {"x": x, "y": y}})

    async def click(
        self,
        x: float,
        y: float,
        button: str = "left",
        count: int = 1,
        expect_window: str | None = None,
    ) -> str:
        from tst_cu_mcp.input_control import click

        try:
            await asyncio.to_thread(click, x, y, button, count, expect_window)
        except Exception as exc:
            raise _map_error(exc) from exc
        return json.dumps({"clicked": {"x": x, "y": y}, "button": button, "count": count})

    async def type_text(self, text: str, expect_window: str | None = None) -> str:
        from tst_cu_mcp.input_control import type_text

        try:
            await asyncio.to_thread(type_text, text, expect_window)
        except Exception as exc:
            raise _map_error(exc) from exc
        return json.dumps({"typed_chars": len(text)})

    async def scroll(
        self,
        dx: int = 0,
        dy: int = 0,
        x: float | None = None,
        y: float | None = None,
        expect_window: str | None = None,
    ) -> str:
        from tst_cu_mcp.input_control import move_mouse, scroll

        try:
            if x is not None and y is not None:
                await asyncio.to_thread(move_mouse, x, y, expect_window)
            await asyncio.to_thread(scroll, dx, dy, expect_window)
        except Exception as exc:
            raise _map_error(exc) from exc
        return json.dumps({"scrolled": {"dx": dx, "dy": dy}})

    async def check_permissions(self) -> dict[str, Any]:
        from tst_cu_mcp.permissions import check_permissions

        try:
            return await asyncio.to_thread(check_permissions, request=False)
        except Exception as exc:
            raise _map_error(exc) from exc

    async def hit_test(self, x: float, y: float) -> dict[str, Any]:
        from tst_cu_mcp.backends import get_backend

        try:
            result = await asyncio.to_thread(get_backend().hit_test, x, y)
        except Exception as exc:
            raise _map_error(exc) from exc
        return result if isinstance(result, dict) else {}

    async def aclose(self) -> None:
        return
