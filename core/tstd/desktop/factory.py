"""Resolve the desktop driver from config (TD-3301).

Empty ``computer_use.command`` is mock-only. A non-empty command is the
stdio argv for ``mcp/tst-cu-mcp``. The daemon owns the child.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from .mcp_driver import McpDesktopDriver
from .mock import MockDesktopDriver
from .protocol import DesktopDriver

if TYPE_CHECKING:
    from ..config import ModelConfig


def argv_from_command(command: str | list[str]) -> list[str]:
    """Normalize a config command to argv. Empty means mock."""
    if isinstance(command, str):
        return shlex.split(command)
    return [str(part) for part in command]


def driver_for_command(command: str | list[str]) -> DesktopDriver:
    argv = argv_from_command(command)
    if not argv:
        return MockDesktopDriver()
    return McpDesktopDriver(argv)


def desktop_driver_from_config(config: ModelConfig) -> DesktopDriver:
    return driver_for_command(config.computer_use.command)
