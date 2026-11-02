"""The machine-wide computer-use stop-file (TD-5223).

``tst-cu-mcp`` checks this file before every input event, so while it
exists every client halts: TST Desk's own daemon and any outside agent
driving the machine through ``tst-cu-mcp``. The Stop button creates it,
Resume removes it, and a file created by hand or by another tool shows in
TST Desk as stopped.

The path resolves exactly as ``tst_cu_mcp.safety.stop_file_path`` does:
``killswitch.stop_file`` in the tst-cu-mcp config, else
``$TST_CU_MCP_STOP_FILE``, else ``~/.tst-cu-mcp/STOP``. This module does
not import that package (the daemon and the server do not share an
install); the file shape is the contract, as for ``cu_policy``.
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml

from .cu_policy import cu_policy_path

STOP_ENV = "TST_CU_MCP_STOP"
STOP_FILE_ENV = "TST_CU_MCP_STOP_FILE"
_TRUTHY = {"1", "true", "yes", "on"}


def _configured_stop_file(config: Path) -> str | None:
    try:
        raw: Any = yaml.safe_load(config.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    killswitch = raw.get("killswitch")
    if not isinstance(killswitch, dict):
        return None
    value = killswitch.get("stop_file")
    if isinstance(value, str) and value.strip():
        return value
    return None


def stop_file_path(config: Path | None = None) -> Path:
    """Where the stop-file lives (same order as tst-cu-mcp)."""
    configured = _configured_stop_file(config if config is not None else cu_policy_path())
    if configured:
        return Path(configured)
    override = os.environ.get(STOP_FILE_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".tst-cu-mcp" / "STOP"


def env_stop_engaged() -> bool:
    """``TST_CU_MCP_STOP`` truthy in this process: stopped, and not clearable here."""
    return os.environ.get(STOP_ENV, "").strip().lower() in _TRUTHY


def stop_engaged(config: Path | None = None) -> bool:
    """True while every computer-use client must refuse actuation."""
    if env_stop_engaged():
        return True
    return stop_file_path(config).exists()


def engage(config: Path | None = None) -> Path:
    """Create the stop-file (and its folder). Returns its path."""
    path = stop_file_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    return path


def clear(config: Path | None = None) -> Path:
    """Remove the stop-file. Absent already is fine. Returns its path."""
    path = stop_file_path(config)
    with suppress(FileNotFoundError):
        path.unlink()
    return path
