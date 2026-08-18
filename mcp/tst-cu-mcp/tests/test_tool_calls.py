"""Tool invocation through a real MCP client.

The SDK's ``Client`` connects to an ``MCPServer`` in-process, so these calls go
through the genuine dispatch path — argument validation, handler invocation,
content-block assembly — without a subprocess or a socket. That covers the half
the stdio test cannot reach without hand-rolling JSON-RPC, which is not how any
client actually talks to this server.

Only non-actuating calls run by default. Anything that would move the pointer or
capture pixels is marked ``desktop``; the error paths are safe because the
refusal happens before the OS is touched.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest
from mcp.client.client import Client

from tst_cu_mcp.server import build_server


async def call(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Call a tool in-process and return its decoded JSON payload."""
    async with Client(build_server(), mode="legacy") as client:
        result = await client.call_tool(name, arguments or {})
    return _payload(result)


async def call_raw(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Call a tool and return the raw result, for inspecting error flags."""
    async with Client(build_server(), mode="legacy") as client:
        return await client.call_tool(name, arguments or {})


def _payload(result: Any) -> Any:
    for block in result.content:
        text = getattr(block, "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    pytest.fail(f"no text content in {result.content!r}")


class TestHealth:
    async def test_health_answers(self) -> None:
        payload = await call("health")
        assert payload["name"] == "tst-cu-mcp"

    async def test_health_reports_this_platform(self) -> None:
        payload = await call("health")
        assert payload["platform"] == sys.platform

    async def test_health_reports_support_and_backend(self) -> None:
        payload = await call("health")
        expected = {"win32": "windows", "darwin": "darwin"}
        assert payload["supported"] is True
        assert payload["backend"] == expected[sys.platform]

    async def test_health_version_matches_the_package(self) -> None:
        from tst_cu_mcp import __version__

        assert (await call("health"))["version"] == __version__


class TestCheckPermissions:
    async def test_reports_this_platforms_shape(self) -> None:
        payload = await call("check_permissions")
        expected = {"win32": "windows", "darwin": "macos"}
        assert payload["platform"] == expected[sys.platform]

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows report shape")
    async def test_windows_discloses_both_silent_limits(self) -> None:
        payload = await call("check_permissions")
        assert set(payload["limits"]) == {"uipi", "secure_desktop"}
        assert payload["all_granted"] is True

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows report shape")
    async def test_windows_reports_elevation(self) -> None:
        assert isinstance((await call("check_permissions"))["elevated"], bool)

    async def test_request_flag_is_accepted(self) -> None:
        # Windows has no prompt to raise, but the argument must still be valid or
        # a client written for macOS breaks when pointed at Windows.
        payload = await call("check_permissions", {"request": False})
        assert payload["all_granted"] in (True, False)


class TestArgumentValidation:
    """Refusals that never reach the OS, so they are safe to run anywhere."""

    async def test_unknown_coordinate_space_is_an_error(self) -> None:
        result = await call_raw("move_mouse", {"x": 1, "y": 1, "coordinate_space": "furlongs"})
        assert result.is_error is True

    async def test_image_space_without_metadata_is_an_error(self) -> None:
        result = await call_raw("move_mouse", {"x": 1, "y": 1, "coordinate_space": "image"})
        assert result.is_error is True

    async def test_unknown_button_is_an_error(self) -> None:
        result = await call_raw(
            "click",
            {"x": 1, "y": 1, "coordinate_space": "points", "button": "middle"},
        )
        assert result.is_error is True

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows key vocabulary")
    async def test_fn_combo_is_refused_on_windows(self) -> None:
        # The port's headline refusal, verified through the tool surface rather
        # than the parser: a mac-only modifier must not silently become a bare key.
        result = await call_raw("press_keys", {"combo": "fn+up"})
        assert result.is_error is True

    async def test_unknown_key_is_an_error(self) -> None:
        result = await call_raw("press_keys", {"combo": "ctrl+nosuchkey"})
        assert result.is_error is True

    async def test_oversized_scroll_is_an_error(self) -> None:
        result = await call_raw("scroll", {"dy": 10_000_000})
        assert result.is_error is True

    async def test_unknown_tool_returns_an_error_result(self) -> None:
        # An error result rather than a raised exception: the model gets something
        # it can read and correct, instead of the connection failing under it.
        result = await call_raw("no_such_tool")
        assert result.is_error is True


class TestToolCatalogue:
    async def test_every_tool_is_listed(self) -> None:
        async with Client(build_server(), mode="legacy") as client:
            listed = {tool.name for tool in (await client.list_tools()).tools}
        assert listed == {
            "health",
            "check_permissions",
            "get_screen_info",
            "screenshot",
            "move_mouse",
            "click",
            "type_text",
            "press_keys",
            "scroll",
            "get_foreground_window",
            "get_cursor_position",
            "wait",
            "wait_for_window",
        }

    async def test_instructions_reach_the_client(self) -> None:
        async with Client(build_server(), mode="legacy") as client:
            instructions = client.instructions or ""
        if sys.platform == "win32":
            assert "Windows" in instructions
            assert "macOS" not in instructions


@pytest.mark.desktop
@pytest.mark.skipif(sys.platform != "win32", reason="native Windows desktop required")
class TestDesktopToolCalls:
    """Calls that read the real display. Non-actuating, but they need a session."""

    async def test_get_screen_info_reports_displays(self) -> None:
        payload = await call("get_screen_info")
        assert payload["count"] >= 1
        assert payload["main_index"] == 0

    async def test_screen_info_bounds_are_populated(self) -> None:
        payload = await call("get_screen_info")
        bounds = payload["displays"][0]["bounds_points"]
        assert bounds["width"] > 0
        assert bounds["height"] > 0

    async def test_screenshot_returns_an_image_and_metadata(self) -> None:
        async with Client(build_server(), mode="legacy") as client:
            result = await client.call_tool("screenshot", {"max_long_edge": 320})

        kinds = [getattr(block, "type", None) for block in result.content]
        assert "image" in kinds
        assert "text" in kinds

    async def test_screenshot_metadata_describes_the_capture(self) -> None:
        payload = await call("screenshot", {"max_long_edge": 320})
        assert payload["image_px"]["width"] <= 320
        assert payload["captured_region_points"]["width"] > 0
        assert "THIS image's pixel space" in payload["coordinate_note"]

    async def test_screenshot_region_is_honoured(self) -> None:
        payload = await call(
            "screenshot",
            {"region": {"x": 0, "y": 0, "width": 128, "height": 96}, "max_long_edge": 0},
        )
        assert payload["image_px"] == {"width": 128, "height": 96}
