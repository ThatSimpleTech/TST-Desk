"""Desktop computer-use drivers for tstd (TD-3301)."""

from .factory import argv_from_command, desktop_driver_from_config, driver_for_command
from .mcp_driver import LIVE_PLATFORMS, MCP_TOOLS, McpDesktopDriver
from .mock import MockDesktopDriver
from .protocol import TINY_PNG, DesktopDriver, DesktopError, window_matches

__all__ = [
    "LIVE_PLATFORMS",
    "MCP_TOOLS",
    "TINY_PNG",
    "DesktopDriver",
    "DesktopError",
    "McpDesktopDriver",
    "MockDesktopDriver",
    "argv_from_command",
    "desktop_driver_from_config",
    "driver_for_command",
    "window_matches",
]
