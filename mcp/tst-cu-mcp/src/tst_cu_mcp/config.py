"""Optional runtime configuration (a scoping seam for future restrictions).

By default no config file exists and every field takes its permissive default —
the server runs whole-desktop and autonomous, as intended for v1. A future
milestone can narrow behavior (e.g. ``scoping.allowed_apps``) without changing
call sites. The file is looked up at ``$TST_CU_MCP_CONFIG`` or
``~/.tst-cu-mcp/config.yaml``; see ``config.example.yaml``.
"""

from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

CuMode = Literal["background", "full_control"]


@dataclass(frozen=True)
class Config:
    """Resolved configuration. Permissive defaults = no restrictions."""

    actuation_enabled: bool = True
    stop_file: str | None = None
    # Real-display glow; the TST_CU_MCP_OVERLAY env var overrides this.
    overlay_enabled: bool = True
    # Empty allowlist means every app except denied. Non-empty is an allowlist.
    allowed_apps: tuple[str, ...] = ()
    denied_apps: tuple[str, ...] = ()
    # background: AX-tree tools, no pointer. full_control: screenshot + click.
    mode: CuMode = "background"
    unhide_on_finish: bool = True


def _default_config_path() -> Path:
    override = os.environ.get("TST_CU_MCP_CONFIG")
    return Path(override) if override else Path.home() / ".tst-cu-mcp" / "config.yaml"


def policy_path() -> Path:
    """The file Settings writes. Tests point TST_CU_MCP_CONFIG at a temp path."""
    return _default_config_path()


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


def save_config(cfg: Config, path: Path | None = None) -> None:
    """Atomically write *cfg* so Settings and the MCP server share one file."""
    import tempfile

    import yaml

    target = path if path is not None else _default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {
        "actuation": {"enabled": cfg.actuation_enabled},
        "overlay": {"enabled": cfg.overlay_enabled},
        "mode": cfg.mode,
        "scoping": {
            "allowed_apps": list(cfg.allowed_apps),
            "denied_apps": list(cfg.denied_apps),
            "unhide_on_finish": cfg.unhide_on_finish,
        },
    }
    if cfg.stop_file:
        body["killswitch"] = {"stop_file": cfg.stop_file}
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(body, handle, sort_keys=False)
        Path(tmp).replace(target)
    except Exception:
        with suppress(OSError):
            Path(tmp).unlink()
        raise


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
        allowed_apps=_string_tuple(scoping.get("allowed_apps")),
        denied_apps=_string_tuple(scoping.get("denied_apps")),
        mode=_mode_flag(raw.get("mode") or scoping.get("mode") or "background", target),
        unhide_on_finish=_bool_flag(
            scoping.get("unhide_on_finish", True), target, "scoping.unhide_on_finish"
        ),
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        item = value.strip()
        return (item,) if item else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise ValueError(f"app list must be a sequence of strings, got {type(value).__name__}")


def _mode_flag(value: object, source: Path) -> CuMode:
    if isinstance(value, str):
        normalized = value.strip().casefold().replace("-", "_")
        if normalized in {"background", "full_control"}:
            return normalized  # type: ignore[return-value]
    raise ValueError(
        f"config at {source}: mode must be 'background' or 'full_control', got {value!r}"
    )


def _bool_flag(value: object, source: Path, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _FLAG_TRUE:
            return True
        if normalized in _FLAG_FALSE:
            return False
    raise ValueError(f"config at {source}: {field} must be true or false, got {value!r}")
