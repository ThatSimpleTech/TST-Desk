"""Live desktop driver: ``mcp/tst-cu-mcp`` over stdio (TD-3301).

macOS, Windows, and Linux X11 are the live platforms (the sidecar's own
backends). Other OS values still refuse with ``e20``. The mock works on
any host.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .permissions import parse_mcp_permissions_result
from .protocol import DesktopError
from .stdio_mcp import StdioMcpClient, map_mcp_error

LIVE_PLATFORMS = frozenset({"darwin", "win32", "linux"})

# Product tools → existing MCP tool names. Points, not last-screenshot
# image space: tstd does not yet thread capture metadata between calls.
MCP_TOOLS = {
    "screenshot": "screenshot",
    "move": "move_mouse",
    "click": "click",
    "type": "type_text",
    "scroll": "scroll",
}


class McpDesktopDriver:
    """Spawn the computer-use MCP server as a stdio child. No socket."""

    def __init__(
        self,
        command: list[str],
        *,
        platform: str | None = None,
        client: StdioMcpClient | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        import sys

        self._command = command
        self._platform = sys.platform if platform is None else platform
        self.platform = self._platform
        self._client = client if client is not None else StdioMcpClient(command, env=env)
        self.killed = False

    def set_killed(self, killed: bool) -> None:
        self.killed = bool(killed)

    async def set_overlay_session(self, active: bool) -> None:
        """Tell the sidecar the computer-use episode opened or closed."""
        try:
            await self._client.call_tool("overlay_session", {"active": bool(active)})
        except Exception:
            return

    def _refuse_if_no_live_path(self) -> None:
        if self._platform in LIVE_PLATFORMS or self._platform.startswith("linux"):
            return
        raise DesktopError(
            "e20",
            f"no live desktop computer-use path for {self._platform!r}; "
            "supported live platforms: darwin, win32, linux",
        )

    def _refuse_if_killed(self) -> None:
        if self.killed:
            raise DesktopError("cu_killed", "computer-use kill-switch is engaged")

    async def check_permissions(self) -> dict[str, Any]:
        """Probe the sidecar. ``request=False`` — never wait on a TCC dialog."""
        try:
            result = await asyncio.wait_for(
                self._client.call_tool("check_permissions", {"request": False}),
                timeout=3.0,
            )
        except TimeoutError:
            return {"all_granted": False, "timed_out": True}
        return parse_mcp_permissions_result(result)

    async def screenshot(self, display: int | None = None) -> str:
        # Capture is allowed on a live path even when the kill-switch is on.
        self._refuse_if_no_live_path()
        arguments: dict[str, Any] = {}
        if display is not None:
            arguments["display"] = display
        result = await self._client.call_tool(MCP_TOOLS["screenshot"], arguments)
        return _screenshot_json(result)

    async def move(self, x: float, y: float, expect_window: str | None = None) -> str:
        self._refuse_if_no_live_path()
        self._refuse_if_killed()
        result = await self._client.call_tool(
            MCP_TOOLS["move"],
            _point_args(x, y, expect_window),
        )
        return json.dumps({"moved_to": {"x": x, "y": y}, "sidecar": result}, default=str)

    async def click(
        self,
        x: float,
        y: float,
        button: str = "left",
        count: int = 1,
        expect_window: str | None = None,
    ) -> str:
        self._refuse_if_no_live_path()
        self._refuse_if_killed()
        args = _point_args(x, y, expect_window)
        args["button"] = button
        args["count"] = count
        result = await self._client.call_tool(MCP_TOOLS["click"], args)
        return json.dumps(
            {"clicked": {"x": x, "y": y}, "button": button, "count": count, "sidecar": result},
            default=str,
        )

    async def type_text(self, text: str, expect_window: str | None = None) -> str:
        self._refuse_if_no_live_path()
        self._refuse_if_killed()
        args: dict[str, Any] = {"text": text}
        if expect_window is not None:
            args["expect_window"] = expect_window
        await self._client.call_tool(MCP_TOOLS["type"], args)
        return json.dumps({"typed_chars": len(text)})

    async def scroll(
        self,
        dx: int = 0,
        dy: int = 0,
        x: float | None = None,
        y: float | None = None,
        expect_window: str | None = None,
    ) -> str:
        self._refuse_if_no_live_path()
        self._refuse_if_killed()
        args: dict[str, Any] = {
            "dx": dx,
            "dy": dy,
            "coordinate_space": "points",
        }
        if x is not None and y is not None:
            args["x"] = x
            args["y"] = y
        if expect_window is not None:
            args["expect_window"] = expect_window
        await self._client.call_tool(MCP_TOOLS["scroll"], args)
        return json.dumps({"scrolled": {"dx": dx, "dy": dy}})

    async def aclose(self) -> None:
        await self._client.aclose()


def _point_args(x: float, y: float, expect_window: str | None) -> dict[str, Any]:
    args: dict[str, Any] = {"x": x, "y": y, "coordinate_space": "points"}
    if expect_window is not None:
        args["expect_window"] = expect_window
    return args


def _screenshot_json(result: Any) -> str:
    """Pull a PNG out of an MCP ``tools/call`` result."""
    content = _content_blocks(result)
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image" and isinstance(block.get("data"), str):
            return json.dumps({"png_base64": block["data"]})
    raise map_mcp_error(f"screenshot returned no image: {result!r}")


def _content_blocks(result: Any) -> list[Any]:
    if isinstance(result, dict):
        if result.get("isError"):
            text = ""
            for block in result.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = str(block.get("text", ""))
                    break
            raise map_mcp_error(text or "computer-use sidecar returned an error")
        raw = result.get("content")
        if isinstance(raw, list):
            return raw
    if isinstance(result, list):
        return result
    return []
