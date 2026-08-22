"""Optional runtime configuration (a scoping seam for future restrictions).

By default no config file exists and every field takes its permissive default —
the server runs whole-desktop and autonomous, as intended for v1. A future
milestone can narrow behavior (e.g. ``scoping.allowed_apps``) without changing
call sites. The file is looked up at ``$TST_CU_MCP_CONFIG`` or
``~/.tst-cu-mcp/config.yaml``; see ``config.example.yaml``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """Resolved configuration. Permissive defaults = no restrictions."""

    actuation_enabled: bool = True
    stop_file: str | None = None
    # Real-display glow; the TST_CU_MCP_OVERLAY env var overrides this.
    overlay_enabled: bool = True
    # Reserved for a future scoping milestone; unused (and unenforced) in v1.
    allowed_apps: tuple[str, ...] = ()


def _default_config_path() -> Path:
    override = os.environ.get("TST_CU_MCP_CONFIG")
    return Path(override) if override else Path.home() / ".tst-cu-mcp" / "config.yaml"


_FLAG_TRUE = {"1", "true", "yes", "on"}
_FLAG_FALSE = {"0", "false", "no", "off"}


def _actuation_flag(value: object, source: Path) -> bool:
    """Coerce ``actuation.enabled`` without ever failing open.

    Generated configs and YAML dialects quote booleans often enough that this
    switch has to survive it: ``bool("false")`` is True in Python, which would
    silently enable actuation for someone who believed they had disabled it.
    A real bool passes through, a recognized string coerces to its literal
    meaning, and anything else refuses to start the server rather than guess
    at a safety setting.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _FLAG_TRUE:
            return True
        if normalized in _FLAG_FALSE:
            return False
    raise ValueError(
        f"config at {source}: actuation.enabled must be true or false "
        f'(a quoted "true"/"false" string is accepted), got {value!r}'
    )


def load_config(path: Path | None = None) -> Config:
    """Load config from ``path`` (or the default location). Absent file -> defaults."""
    target = path if path is not None else _default_config_path()
    if not target.exists():
        return Config()

    import yaml

    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config at {target} must be a mapping")

    actuation = raw.get("actuation") or {}
    killswitch = raw.get("killswitch") or {}
    overlay = raw.get("overlay") or {}
    scoping = raw.get("scoping") or {}
    return Config(
        actuation_enabled=_actuation_flag(actuation.get("enabled", True), target),
        stop_file=killswitch.get("stop_file"),
        overlay_enabled=bool(overlay.get("enabled", True)),
        allowed_apps=tuple(scoping.get("allowed_apps") or ()),
    )
