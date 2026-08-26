"""Desktop computer-use drivers for tstd (TD-3301)."""

from .factory import argv_from_command, desktop_driver_from_config, driver_for_command
from .mcp_driver import LIVE_PLATFORMS, MCP_TOOLS, McpDesktopDriver
from .mock import MockDesktopDriver
from .permissions import ACCESSIBILITY_URL, SCREEN_RECORDING_URL
from .protocol import TINY_PNG, DesktopDriver, DesktopError, scripted_ax_hit_node, window_matches

__all__ = [
    "ACCESSIBILITY_URL",
    "LIVE_PLATFORMS",
    "MCP_TOOLS",
    "SCREEN_RECORDING_URL",
    "TINY_PNG",
    "DesktopDriver",
    "DesktopError",
    "McpDesktopDriver",
    "MockDesktopDriver",
    "argv_from_command",
    "desktop_driver_from_config",
    "driver_for_command",
    "scripted_ax_hit_node",
    "window_matches",
]
