"""Browser computer-use (TD-1710). First-party driver; ``files_102.zip`` was absent."""

from .candidates import Candidate, candidate_from_node, render_candidates, select_candidate
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
    "Candidate",
    "MockBrowserDriver",
    "PlaywrightBrowserDriver",
    "browser_driver_from_config",
    "candidate_from_node",
    "normalize_hit",
    "playwright_available",
    "png_size",
    "render_candidates",
    "scripted_hit_node",
    "select_candidate",
]
