"""Computer-use policy persisted for the MCP server (TD-4830).

The file is ``~/.tst-cu-mcp/config.yaml`` — the same document
``tst-cu-mcp`` re-reads on every background tool call. This module does
not import that package: tstd's tests and the sidecar do not share an
install. The YAML shape is the contract.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

CuMode = Literal["background", "full_control"]


@dataclass(frozen=True)
class CuPolicy:
    enabled: bool = True
    mode: CuMode = "background"
    unhide_on_finish: bool = True
    denied_apps: tuple[str, ...] = ()
    allowed_apps: tuple[str, ...] = ()


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
    except (OSError, yaml.YAMLError):
        return CuPolicy()
    if not isinstance(raw, dict):
        return CuPolicy()
    actuation = _as_map(raw.get("actuation"))
    scoping = _as_map(raw.get("scoping"))
    mode_raw = raw.get("mode") or scoping.get("mode") or "background"
    mode: CuMode = "full_control" if str(mode_raw).strip() == "full_control" else "background"
    enabled_raw = actuation.get("enabled", True)
    if isinstance(enabled_raw, str):
        enabled = enabled_raw.strip().lower() in {"1", "true", "yes", "on"}
    else:
        enabled = enabled_raw is not False
    return CuPolicy(
        enabled=enabled,
        mode=mode,
        unhide_on_finish=scoping.get("unhide_on_finish", True) is not False,
        denied_apps=_string_tuple(scoping.get("denied_apps")),
        allowed_apps=_string_tuple(scoping.get("allowed_apps")),
    )


def save_cu_policy(policy: CuPolicy, path: Path | None = None) -> None:
    target = path if path is not None else cu_policy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if target.exists():
        try:
            loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, yaml.YAMLError):
            existing = {}
    overlay = _as_map(existing.get("overlay"))
    killswitch = _as_map(existing.get("killswitch"))
    body: dict[str, Any] = {
        "actuation": {"enabled": policy.enabled},
        "overlay": {"enabled": overlay.get("enabled", True)},
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
