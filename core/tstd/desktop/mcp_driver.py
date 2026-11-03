"""Live desktop driver: ``mcp/tst-cu-mcp`` over stdio (TD-3301).

macOS, Windows, and Linux X11 are the live platforms (the sidecar's own
backends). Other OS values still refuse with ``e20``. The mock works on
any host.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from ..cu_agents import CuAgent, current_cu_agent
from .permissions import parse_mcp_permissions_result
from .protocol import DesktopError
from .stdio_mcp import StdioMcpClient, map_mcp_error

LIVE_PLATFORMS = frozenset({"darwin", "win32", "linux"})
_T = TypeVar("_T")

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
        # TD-5227: which session's cursor the sidecar draws next.
        self._agent_lock = asyncio.Lock()
        self._announced: tuple[str, str] | None = None
        self._colors: dict[str, str] = {}

    def set_killed(self, killed: bool) -> None:
        self.killed = bool(killed)

    async def agent_color(self, agent: CuAgent) -> str | None:
        """Name ``agent`` to the sidecar; its cursor color, or None if unknown."""
        async with self._agent_lock:
            return await self._announce(agent)

    async def _announce(self, agent: CuAgent) -> str | None:
        label = agent.label
        if self._announced == (agent.agent_id, label):
            return self._colors.get(agent.agent_id)
        try:
            result = await self._client.call_tool(
                "overlay_agent", {"agent_id": agent.agent_id, "label": label}
            )
            color = _agent_color(result)
        except Exception:
            return None
        self._announced = (agent.agent_id, label)
        if color is not None:
            self._colors[agent.agent_id] = color
        return color

    async def _as_agent(self, act: Callable[[], Awaitable[_T]]) -> _T:
        """Run one pointer action as the acting session (TD-5227).

        The sidecar draws the cursor of whichever agent it was last told
        about, so naming the agent and acting happen under one lock: two
        sessions acting at once cannot swap each other's cursors.
        """
        agent = current_cu_agent.get()
        async with self._agent_lock:
            if agent is not None:
                await self._announce(agent)
            return await act()

    async def set_overlay_session(self, active: bool, session: str = "") -> None:
        """Tell the sidecar one session's computer-use episode opened or closed."""
        try:
            await self._client.call_tool(
                "overlay_session", {"active": bool(active), "session": session}
            )
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
        result = await self._as_agent(
            lambda: self._client.call_tool(MCP_TOOLS["move"], _point_args(x, y, expect_window))
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
        result = await self._as_agent(lambda: self._client.call_tool(MCP_TOOLS["click"], args))
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
        await self._as_agent(lambda: self._client.call_tool(MCP_TOOLS["scroll"], args))
        return json.dumps({"scrolled": {"dx": dx, "dy": dy}})

    async def foreground_window(self) -> dict[str, Any]:
        # Read-only, like screenshot. The fixed message is deliberate:
        # a sidecar error can quote a window title or a path.
        self._refuse_if_no_live_path()
        try:
            result = await self._client.call_tool("get_foreground_window", {})
        except Exception as exc:
            raise DesktopError("cu_error", "foreground window was not read") from exc
        return _hit_json(result)

    async def hit_test(self, x: float, y: float) -> dict[str, Any]:
        # Observe only: kill-switch must not block this, same as screenshot.
        self._refuse_if_no_live_path()
        result = await self._client.call_tool(
            "hit_test",
            {"x": x, "y": y, "coordinate_space": "image"},
        )
        return _hit_json(result)

    async def aclose(self) -> None:
        await self._client.aclose()


def _agent_color(result: Any) -> str | None:
    """The ``color`` from an ``overlay_agent`` reply (a JSON text block)."""
    for block in _content_blocks(result):
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            continue
        color = body.get("color") if isinstance(body, dict) else None
        if isinstance(color, str) and len(color) == 7 and color.startswith("#"):
            return color
    return None


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


def _hit_json(result: Any) -> dict[str, Any]:
    """Pull a Design-mode node out of an MCP ``tools/call`` result."""
    for block in _content_blocks(result):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


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
