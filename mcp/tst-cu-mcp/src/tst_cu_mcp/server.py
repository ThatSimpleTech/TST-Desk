"""Construct and run the tst-cu-mcp MCP server.

The server speaks the MCP stdio transport only: it is launched as a subprocess
by an MCP client (Kiro, Claude Desktop, Goose) and never binds a network socket.
Tools are registered here; their logic lives in :mod:`tst_cu_mcp.tools`.
"""

from __future__ import annotations

import base64
import json
import sys
from typing import Any

from mcp.server import MCPServer
from mcp.types import ContentBlock, ImageContent, TextContent

from tst_cu_mcp import input_control, waiting
from tst_cu_mcp._version import __version__
from tst_cu_mcp.capture import DEFAULT_MAX_LONG_EDGE, capture, parse_region_dict
from tst_cu_mcp.coordinates import resolve_point
from tst_cu_mcp.displays import screen_info
from tst_cu_mcp.focus import foreground_window
from tst_cu_mcp.permissions import check_permissions
from tst_cu_mcp.stdio_transport import DrainingStdioServer
from tst_cu_mcp.tools.health import health_report

# The model reads these strings and acts on them, so they must describe the host
# it is actually driving. A hardcoded "macOS" here told every Windows model to
# reach for cmd-shortcuts and to expect permission prompts that do not exist.

_BASE_INSTRUCTIONS = (
    "Local computer-use server. Use `health` for liveness and `check_permissions` "
    "for this platform's capture/input status before capturing the screen or "
    "driving input. This server can see the whole desktop and control mouse and "
    "keyboard; there is no per-action approval gate."
)

_PLATFORM_INSTRUCTIONS = {
    "darwin": (
        " Host: macOS. Screen Recording and Accessibility must be granted to the "
        "launching app (TST Desk, Terminal, ...), not this server. Call "
        "check_permissions with request=false to probe. request=true raises each "
        "OS prompt at most once — do not pass it again; clicking Allow repeatedly "
        "does not stick. After enabling the host in System Settings, the host "
        "must be fully quit and reopened (Cmd+Q). If check_permissions reports "
        "stale_grant_suspected, stop: no amount of request=true will help; tell "
        "the user to run Reset grants in TST Desk → Settings → Computer use, "
        "relaunch, and allow again. Never run the screencapture "
        "shell command — it re-prompts Screen Recording; use the screenshot tool. "
        "Shortcut modifiers: cmd, shift, alt/option, ctrl, fn."
    ),
    "win32": (
        " Host: Windows. No permissions to grant. Shortcut modifiers: ctrl, shift, "
        "alt, win/super — `cmd` is accepted as an alias for ctrl. Two silent "
        "failure modes: windows running elevated discard synthetic input, and the "
        "secure desktop (UAC prompt, lock screen) can be neither captured nor "
        "driven."
    ),
    "linux": (
        " Host: Linux. An X11 session is required. Wayland cannot be captured or "
        "driven (TD-2002, not in current milestones); call `health` — if "
        "session_type is wayland, stop. "
        "No permissions to grant on X11. Shortcut modifiers: ctrl, shift, alt, "
        "win/super — `cmd` is accepted as an alias for ctrl. `fn` is refused."
    ),
}


def instructions(platform: str | None = None) -> str:
    """Server instructions, describing the platform actually being driven."""
    target = sys.platform if platform is None else platform
    return _BASE_INSTRUCTIONS + _PLATFORM_INSTRUCTIONS.get(
        target, " Host platform is not supported by this server; see `health`."
    )


def _permission_hint(platform: str | None = None) -> str:
    """One-line permission caveat for tool descriptions."""
    target = sys.platform if platform is None else platform
    if target == "darwin":
        return "Requires Screen Recording (capture) or Accessibility (input) permission."
    if target == "win32":
        return "No permission needed; elevated windows silently discard input."
    if target.startswith("linux"):
        return "X11 only; Wayland is unsupported. No permission grant on X11."
    return "Call check_permissions for this platform's status."


def _combo_help(platform: str | None = None) -> str:
    """Key-combo vocabulary for this platform, listing the modifiers that exist."""
    target = sys.platform if platform is None else platform
    if target == "darwin":
        return (
            "Press a key combo, e.g. 'cmd+c', 'cmd+shift+t', 'return', 'escape', 'up'. "
            "Modifiers: cmd/command, shift, alt/option, ctrl/control, fn. "
            "Separate with '+'. "
        )
    if target == "win32":
        return (
            "Press a key combo, e.g. 'ctrl+c', 'ctrl+shift+t', 'return', 'escape', 'up'. "
            "Modifiers: ctrl/control, shift, alt/option, win/super. 'cmd'/'command' are "
            "accepted as aliases for ctrl, so mac-style combos work. 'fn' does not exist "
            "on Windows and is refused. Separate with '+'. "
        )
    if target.startswith("linux"):
        return (
            "Press a key combo, e.g. 'ctrl+c', 'ctrl+shift+t', 'return', 'escape', 'up'. "
            "Modifiers: ctrl/control, shift, alt/option, win/super. 'cmd'/'command' are "
            "accepted as aliases for ctrl, so mac-style combos work. 'fn' does not exist "
            "on X11 and is refused. Separate with '+'. "
        )
    return "Press a key combo, separating tokens with '+'. "


def build_server() -> MCPServer:
    """Build the MCPServer with all tools registered.

    Returns a fresh instance each call so tests get isolated servers.
    """
    # DrainingStdioServer swaps in a stdio transport that answers every
    # request received before EOF; the stock one tears down on EOF and can
    # cancel a just-spawned handler from a batched client mid-flight (TD-4822).
    server: MCPServer = DrainingStdioServer(
        name="tst-cu-mcp",
        version=__version__,
        instructions=instructions(),
    )

    @server.tool(
        name="health",
        description=(
            "Liveness check. Returns server name, version, the platform detected, "
            "the active backend, and whether this platform is supported."
        ),
        structured_output=False,
    )
    def health() -> dict[str, Any]:
        return health_report()

    @server.tool(
        name="check_permissions",
        description=(
            "Report this platform's capture and input status. On macOS: Screen "
            "Recording and Accessibility grants for the host app, with fix steps "
            "and the host-restart caveat. Prefer request=false. request=true raises "
            "each OS prompt at most once; later calls only re-probe — do not keep "
            "passing request=true. stale_grant_suspected means System Settings "
            "shows the host ON for an older build: only a reset by the user "
            "(TST Desk → Settings → Computer use → Reset grants) repairs it. On "
            "Windows: nothing is gated, so it reports the "
            "two conditions under which input silently does nothing — elevated "
            "windows (UIPI) and the secure desktop. Safe to call anytime."
        ),
        structured_output=False,
    )
    def check_permissions_tool(request: bool = False) -> dict[str, Any]:
        return check_permissions(request=request)

    @server.tool(
        name="get_screen_info",
        description=(
            "List active displays with their global bounds, scale factor, and "
            "which is the main display. Use this to choose a "
            "display index and to reason about multi-monitor layout before "
            "capturing or clicking."
        ),
        structured_output=False,
    )
    def get_screen_info_tool() -> dict[str, Any]:
        return screen_info()

    @server.tool(
        name="screenshot",
        description=(
            "Capture the screen and return a PNG (the model's eyes). Defaults to the "
            "main display. Pass `display` (index from get_screen_info) to target a "
            "specific monitor. Pass `region` as display-local coordinates "
            "{x, y, width, height} where 0,0 is that display's top-left, to zoom in on "
            "a corner. `max_long_edge` bounds the returned image's longer side. The "
            "returned text block carries coordinate metadata: click/move coordinates "
            "must be given in the RETURNED IMAGE's pixel space (origin top-left). "
            + _permission_hint()
        ),
        structured_output=False,
    )
    def screenshot(
        display: int | None = None,
        region: dict[str, int] | None = None,
        max_long_edge: int = DEFAULT_MAX_LONG_EDGE,
    ) -> list[ContentBlock]:
        region_tuple = parse_region_dict(region)
        result = capture(
            display_index=display,
            region=region_tuple,
            max_long_edge=max_long_edge,
        )
        return [
            ImageContent(
                type="image",
                data=base64.b64encode(result.png_bytes).decode("ascii"),
                mime_type="image/png",
            ),
            TextContent(type="text", text=json.dumps(result.metadata())),
        ]

    _COORD_HELP = (
        "Coordinates: with coordinate_space='image' (default) x,y are pixels in the "
        "last screenshot; you must also pass image_width, image_height and region "
        "(the screenshot metadata's image_px and captured_region_points). With "
        "coordinate_space='points', x,y are global coordinates. " + _permission_hint()
    )

    _EXPECT_HELP = (
        " Pass `expect_window` with part of the intended window's title or process "
        "name (case-insensitive) and the action is refused unless that window is "
        "in front. Strongly recommended: a coordinate aims at a point, not at a "
        "thing, so if the UI moved since your screenshot the action would otherwise "
        "land on whatever is there now."
    )

    @server.tool(
        name="get_foreground_window",
        description=(
            "Report the window currently in front: title, process name, pid and "
            "bounds. Use it to confirm you are about to act on the right thing, and "
            "to diagnose input that appears to succeed but has no effect."
        ),
        structured_output=False,
    )
    def get_foreground_window() -> dict[str, Any]:
        return foreground_window().to_dict()

    @server.tool(
        name="get_cursor_position",
        description=(
            "Report the pointer's current global coordinates. Works while the "
            "kill-switch is engaged, since it only reads."
        ),
        structured_output=False,
    )
    def get_cursor_position() -> dict[str, Any]:
        x, y = input_control.cursor_position()
        return {"cursor_points": {"x": x, "y": y}}

    @server.tool(
        name="wait",
        description=(
            "Sleep for a number of seconds (max 30) to let the UI settle. Prefer "
            "wait_for_window when you are waiting for something specific."
        ),
        structured_output=False,
    )
    def wait(seconds: float = 1.0) -> dict[str, Any]:
        return waiting.wait(seconds)

    @server.tool(
        name="wait_for_window",
        description=(
            "Poll until a window whose title or process name contains `title` "
            "(case-insensitive) is in front, or until timeout. Use this after "
            "launching an application instead of guessing a sleep duration. "
            "Returns matched=false plus what is actually in front rather than "
            "failing, so a timeout is diagnosable."
        ),
        structured_output=False,
    )
    def wait_for_window(title: str, timeout_seconds: float = 10.0) -> dict[str, Any]:
        return waiting.wait_for_window(title, timeout_seconds)

    @server.tool(
        name="move_mouse",
        description="Move the mouse cursor to a point. " + _COORD_HELP + _EXPECT_HELP,
        structured_output=False,
    )
    def move_mouse(
        x: float,
        y: float,
        coordinate_space: str = "image",
        image_width: int | None = None,
        image_height: int | None = None,
        region: dict[str, int] | None = None,
        expect_window: str | None = None,
    ) -> dict[str, Any]:
        gx, gy = resolve_point(
            x=x,
            y=y,
            coordinate_space=coordinate_space,
            image_width=image_width,
            image_height=image_height,
            region=region,
        )
        input_control.move_mouse(gx, gy, expect_window=expect_window)
        return {"moved_to_points": {"x": round(gx, 1), "y": round(gy, 1)}}

    @server.tool(
        name="click",
        description=(
            "Click the mouse at a point. button is 'left' or 'right'; count>=2 "
            "double/triple-clicks. " + _COORD_HELP + _EXPECT_HELP
        ),
        structured_output=False,
    )
    def click(
        x: float,
        y: float,
        button: str = "left",
        count: int = 1,
        coordinate_space: str = "image",
        image_width: int | None = None,
        image_height: int | None = None,
        region: dict[str, int] | None = None,
        expect_window: str | None = None,
    ) -> dict[str, Any]:
        gx, gy = resolve_point(
            x=x,
            y=y,
            coordinate_space=coordinate_space,
            image_width=image_width,
            image_height=image_height,
            region=region,
        )
        input_control.click(gx, gy, button=button, count=count, expect_window=expect_window)
        return {
            "clicked_points": {"x": round(gx, 1), "y": round(gy, 1)},
            "button": button,
            "count": count,
        }

    @server.tool(
        name="type_text",
        description=(
            "Type a Unicode string at the current keyboard focus (does not move "
            "focus — click a field first if needed). The typed text is never logged. "
            + _permission_hint()
            + _EXPECT_HELP
            + " Note that a window which has only just appeared may not be ready to "
            "receive keystrokes even when it is in front; keys sent too early are "
            "discarded by the receiving window, and this tool cannot detect that. "
            "Screenshot to confirm the caret before typing anything that matters."
        ),
        structured_output=False,
    )
    def type_text(text: str, expect_window: str | None = None) -> dict[str, Any]:
        input_control.type_text(text, expect_window=expect_window)
        return {"typed_chars": len(text)}

    @server.tool(
        name="press_keys",
        description=(_combo_help() + _permission_hint() + _EXPECT_HELP),
        structured_output=False,
    )
    def press_keys(combo: str, expect_window: str | None = None) -> dict[str, Any]:
        input_control.press_keys(combo, expect_window=expect_window)
        return {"pressed": combo}

    @server.tool(
        name="scroll",
        description=(
            "Scroll by lines: dy>0 scrolls up, dy<0 down; dx>0 right, dx<0 left. "
            "Optionally pass x,y to move the cursor over a target first (same "
            "coordinate rules as click). " + _permission_hint() + _EXPECT_HELP
        ),
        structured_output=False,
    )
    def scroll(
        dx: int = 0,
        dy: int = 0,
        x: float | None = None,
        y: float | None = None,
        coordinate_space: str = "image",
        image_width: int | None = None,
        image_height: int | None = None,
        region: dict[str, int] | None = None,
        expect_window: str | None = None,
    ) -> dict[str, Any]:
        if x is not None and y is not None:
            gx, gy = resolve_point(
                x=x,
                y=y,
                coordinate_space=coordinate_space,
                image_width=image_width,
                image_height=image_height,
                region=region,
            )
            input_control.move_mouse(gx, gy, expect_window=expect_window)
        input_control.scroll(dx, dy, expect_window=expect_window)
        return {"scrolled": {"dx": dx, "dy": dy}}

    @server.tool(
        name="overlay_session",
        description=(
            "Internal TST Desk signal: computer-use episode open/close. "
            "Not a model tool. Lights or darkens the real-display ring."
        ),
        structured_output=False,
    )
    def overlay_session(active: bool) -> dict[str, Any]:
        from tst_cu_mcp.overlay import get_overlay

        if active:
            get_overlay().begin_session()
        else:
            get_overlay().end_session()
        return {"active": bool(active)}

    return server


def run() -> None:
    """Build the server and serve forever over stdio."""
    from tst_cu_mcp import safety
    from tst_cu_mcp.config import load_config
    from tst_cu_mcp.logging_setup import configure_logging
    from tst_cu_mcp.overlay import get_overlay

    configure_logging()
    safety.set_config(load_config())
    try:
        build_server().run(transport="stdio")
    finally:
        # Release whatever the overlay holds (the darwin helper child); EOF
        # already covers a crashed parent, this covers a graceful exit.
        get_overlay().shutdown()
