"""Browser computer-use (TD-1710). First-party driver; ``files_102.zip`` was absent."""

from .factory import browser_driver_from_config
from .mock import MockBrowserDriver
from .playwright_driver import PlaywrightBrowserDriver, playwright_available
from .protocol import (
    TINY_PNG,
    TINY_PNG_B64,
    BrowserDriver,
    BrowserError,
    normalize_hit,
    png_size,
    scripted_hit_node,
)

__all__ = [
    "TINY_PNG",
    "TINY_PNG_B64",
    "BrowserDriver",
    "BrowserError",
    "MockBrowserDriver",
    "PlaywrightBrowserDriver",
    "browser_driver_from_config",
    "normalize_hit",
    "playwright_available",
    "png_size",
    "scripted_hit_node",
]
