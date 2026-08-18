"""The single actuation guard: a kill-switch checked before every input event.

Since v1 is whole-desktop and autonomous (no per-action approval), this is the
safety net. Actuation is refused if any of these hold:

* config ``actuation.enabled`` is false,
* the env var ``TST_CU_MCP_STOP`` is truthy, or
* a stop-file exists (``$TST_CU_MCP_STOP_FILE`` or ``~/.tst-cu-mcp/STOP``).

Every public actuation function calls :func:`ensure_actuation_allowed` first, so
dropping the stop-file (or ``touch``-ing it) halts input immediately.
"""

from __future__ import annotations

import os
from pathlib import Path

from tst_cu_mcp.config import Config

STOP_ENV = "TST_CU_MCP_STOP"
STOP_FILE_ENV = "TST_CU_MCP_STOP_FILE"
DEFAULT_STOP_FILE = Path.home() / ".tst-cu-mcp" / "STOP"
_TRUTHY = {"1", "true", "yes", "on"}


class KillSwitchEngaged(RuntimeError):
    """Raised when actuation is blocked by the kill-switch or config."""


_config: Config | None = None


def set_config(config: Config | None) -> None:
    """Install the active config (called once at startup)."""
    global _config
    _config = config


def _active_config() -> Config:
    return _config if _config is not None else Config()


def stop_file_path(config: Config | None = None) -> Path:
    cfg = config if config is not None else _active_config()
    if cfg.stop_file:
        return Path(cfg.stop_file)
    override = os.environ.get(STOP_FILE_ENV)
    return Path(override) if override else DEFAULT_STOP_FILE


def _env_stop_engaged() -> bool:
    return os.environ.get(STOP_ENV, "").strip().lower() in _TRUTHY


def killswitch_engaged() -> bool:
    """True if actuation is currently blocked for any reason."""
    if not _active_config().actuation_enabled:
        return True
    if _env_stop_engaged():
        return True
    return stop_file_path().exists()


def ensure_actuation_allowed() -> None:
    """Raise :class:`KillSwitchEngaged` if actuation is currently blocked."""
    cfg = _active_config()
    if not cfg.actuation_enabled:
        raise KillSwitchEngaged("actuation is disabled in config (actuation.enabled = false)")
    if _env_stop_engaged():
        raise KillSwitchEngaged(
            f"actuation halted by kill-switch env {STOP_ENV}; unset it to resume"
        )
    path = stop_file_path(cfg)
    if path.exists():
        raise KillSwitchEngaged(
            f"actuation halted by kill-switch stop-file {path}; delete it to resume"
        )
