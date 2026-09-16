"""Resolve the desktop driver from config (TD-3301).

Empty ``computer_use.command`` is mock-only in checkout/CI. Packaged
Linux runs the X11/Wayland backends in-process (TD-1727). Packaged
macOS/Windows still spawn ``tstd --cu-mcp``. A non-empty command is
stdio argv for ``mcp/tst-cu-mcp``.
"""

from __future__ import annotations

import shlex
import sys
from typing import TYPE_CHECKING

from .mcp_driver import McpDesktopDriver
from .mock import MockDesktopDriver
from .protocol import DesktopDriver

if TYPE_CHECKING:
    from ..config import ModelConfig
    from ..cu_indicators import CuIndicatorPrefs

#: Sidecar env var that turns the real-display glow on or off. The daemon
#: sets it from the user's ``show_on_real_display`` pref at spawn time, so
#: the sidecar never has to guess (and an explicit value beats its own
#: config default). Mirrors tst_cu_mcp.overlay.OVERLAY_ENV without a
#: cross-package import.
OVERLAY_ENV = "TST_CU_MCP_OVERLAY"

#: Sidecar env var that unlocks the internal tools — today just
#: ``overlay_session``, the episode bracket this driver drives. Grok spawns its
#: own copy of the same binary without this set, so the model's tool list never
#: carries a control the daemon owns. Mirrors tst_cu_mcp.server.INTERNAL_ENV
#: without a cross-package import.
INTERNAL_ENV = "TST_CU_MCP_INTERNAL"


def argv_from_command(command: str | list[str]) -> list[str]:
    """Normalize a config command to argv. Empty means mock."""
    if isinstance(command, str):
        return shlex.split(command)
    return [str(part) for part in command]


def packaged_cu_argv() -> list[str]:
    """``tstd --cu-mcp`` when this process is the packaged sidecar (TD-1725)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--cu-mcp"]
    return []


def driver_for_command(
    command: str | list[str], env: dict[str, str] | None = None
) -> DesktopDriver:
    argv = argv_from_command(command)
    if not argv:
        argv = packaged_cu_argv()
    if not argv:
        return MockDesktopDriver()
    return McpDesktopDriver(argv, env=env)


def desktop_driver_from_config(
    config: ModelConfig, cu_prefs: CuIndicatorPrefs | None = None
) -> DesktopDriver:
    command: str | list[str] = config.computer_use.command
    argv = argv_from_command(command)
    if not argv and _linux_inprocess():
        from .inprocess import InProcessDesktopDriver

        return InProcessDesktopDriver()
    if not argv:
        argv = packaged_cu_argv()
    env: dict[str, str] | None = None
    if argv:
        # This child is ours: it gets the internal tools whether or not the
        # user wants the ring painted.
        env = {INTERNAL_ENV: "1"}
        if cu_prefs is not None:
            env[OVERLAY_ENV] = "1" if cu_prefs.show_on_real_display else "0"
        command = argv
    return driver_for_command(command, env=env)


def _linux_inprocess() -> bool:
    """Packaged Linux: run X11/Wayland backends here, do not spawn tstd."""
    return bool(getattr(sys, "frozen", False)) and sys.platform.startswith("linux")
