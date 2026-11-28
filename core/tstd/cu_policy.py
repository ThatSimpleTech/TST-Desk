"""Computer-use policy persisted for the MCP server (TD-4830).

The file is ``~/.tst-cu-mcp/config.yaml`` — the same document
``tst-cu-mcp`` re-reads on every background tool call. This module does
not import that package: tstd's tests and the sidecar do not share an
install. The YAML shape is the contract.

A file that cannot be read, or that says something this loader does not
understand, reads as Off (CU-20): never the permissive defaults. Only a
missing file is a fresh install. The reason travels with the policy so
Settings can say so, and the next save moves the damaged file aside
(``config.yaml.damaged-<stamp>``) instead of writing over it.
"""

from __future__ import annotations

import os
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from .data_version import read_only

CuMode = Literal["background", "full_control"]


@dataclass(frozen=True)
class CuPolicy:
    enabled: bool = True
    mode: CuMode = "background"
    unhide_on_finish: bool = True
    denied_apps: tuple[str, ...] = ()
    allowed_apps: tuple[str, ...] = ()
    #: Why the file could not be trusted, or None. A damaged file reads as Off.
    damaged: str | None = None


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _damaged(reason: str) -> CuPolicy:
    return CuPolicy(enabled=False, damaged=reason)


def cu_policy_path() -> Path:
    override = os.environ.get("TST_CU_MCP_CONFIG", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".tst-cu-mcp" / "config.yaml"


def load_cu_policy(path: Path | None = None) -> CuPolicy:
    target = path if path is not None else cu_policy_path()
    if not target.exists():
        return CuPolicy()
    try:
        raw: Any = yaml.safe_load(target.read_text(encoding="utf-8"))
    except OSError as exc:
        return _damaged(f"the file could not be read ({exc.strerror or exc})")
    except yaml.YAMLError:
        return _damaged("the file is not valid YAML")
    if raw is None:
        return CuPolicy()
    if not isinstance(raw, dict):
        return _damaged("the file does not hold a mapping")
    actuation = _as_map(raw.get("actuation"))
    scoping = _as_map(raw.get("scoping"))
    mode_raw = raw.get("mode") or scoping.get("mode") or "background"
    mode: CuMode = "full_control" if str(mode_raw).strip() == "full_control" else "background"
    enabled = _flag(actuation.get("enabled", True))
    if enabled is None:
        return _damaged("actuation.enabled is not true or false")
    lists: dict[str, tuple[str, ...]] = {}
    for key in ("denied_apps", "allowed_apps"):
        value = scoping.get(key)
        if value is not None and not isinstance(value, (list, str)):
            return _damaged(f"scoping.{key} is not a list of apps")
        lists[key] = _string_tuple(value)
    return CuPolicy(
        enabled=enabled,
        mode=mode,
        unhide_on_finish=scoping.get("unhide_on_finish", True) is not False,
        denied_apps=lists["denied_apps"],
        allowed_apps=lists["allowed_apps"],
    )


def _flag(value: object) -> bool | None:
    """True or False as YAML or a person might write it; None when unclear."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return {0: False, 1: True}.get(value)
    if isinstance(value, str):
        word = value.strip().lower()
        return True if word in _TRUE else False if word in _FALSE else None
    return None


def _set_aside(target: Path) -> None:
    """Keep a file this module cannot read, under a name nothing reads."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    with suppress(OSError):
        target.replace(target.with_name(f"{target.name}.damaged-{stamp}"))


def save_cu_policy(policy: CuPolicy, path: Path | None = None) -> None:
    if read_only() is not None:  # UPGRADE-02: a newer version's folder
        return
    target = path if path is not None else cu_policy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if target.exists():
        if load_cu_policy(target).damaged is not None:
            _set_aside(target)
        else:
            loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
    overlay = _as_map(existing.get("overlay"))
    killswitch = _as_map(existing.get("killswitch"))
    body: dict[str, Any] = {
        "actuation": {"enabled": policy.enabled},
        "overlay": {
            "enabled": overlay.get("enabled", True),
            "cursor": overlay.get("cursor", True),
        },
        "mode": policy.mode,
        "scoping": {
            "allowed_apps": list(policy.allowed_apps),
            "denied_apps": list(policy.denied_apps),
            "unhide_on_finish": policy.unhide_on_finish,
        },
    }
    if killswitch.get("stop_file"):
        body["killswitch"] = {"stop_file": killswitch["stop_file"]}
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(body, handle, sort_keys=False)
        Path(tmp).replace(target)
    except Exception:
        with suppress(OSError):
            Path(tmp).unlink()
        raise


def save_overlay_prefs(*, enabled: bool, cursor: bool, path: Path | None = None) -> bool:
    """Mirror TST Desk's display switches into the shared policy file (TD-5227).

    ``overlay.enabled`` is "Show on the real display" and ``overlay.cursor``
    is "Agent cursor". Every tst-cu-mcp client re-reads them when the file
    changes, so the switches apply to all of them, not only TST Desk's own.
    Every other key is kept as it is. Returns True when the file changed.
    """
    if read_only() is not None:  # UPGRADE-02: a newer version's folder
        return False
    target = path if path is not None else cu_policy_path()
    existing: dict[str, Any] = {}
    if target.exists():
        try:
            loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return False
        if loaded is not None and not isinstance(loaded, dict):
            return False
        existing = loaded or {}
    overlay = _as_map(existing.get("overlay"))
    if overlay.get("enabled", True) is enabled and overlay.get("cursor", True) is cursor:
        return False
    existing["overlay"] = {**overlay, "enabled": enabled, "cursor": cursor}
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(existing, handle, sort_keys=False)
        Path(tmp).replace(target)
    except Exception:
        with suppress(OSError):
            Path(tmp).unlink()
        raise
    return True


def _as_map(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        item = value.strip()
        return (item,) if item else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()
