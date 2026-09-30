"""Resolve the browser driver from config (TD-1710).

``computer_use.browser`` is ``mock`` (default) or ``playwright``. Missing
Playwright, or an explicit mock, is the in-process TD-102 driver — never
a Chrome launch.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .mock import MockBrowserDriver
from .playwright_driver import PlaywrightBrowserDriver, playwright_available
from .protocol import BrowserDriver

if TYPE_CHECKING:
    from ..config import ModelConfig


def browser_profile_dir(data_dir: Path) -> Path:
    """Persistent Playwright profile. Never the workspace."""
    return data_dir / "browser-profile"


def browser_driver_from_config(config: ModelConfig, data_dir: Path) -> BrowserDriver:
    """Return the configured browser driver. Never binds a socket."""
    if config.computer_use.browser == "playwright" and playwright_available():
        return PlaywrightBrowserDriver(browser_profile_dir(data_dir))
    return MockBrowserDriver()
