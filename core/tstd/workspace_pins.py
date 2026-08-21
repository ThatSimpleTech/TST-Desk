"""Machine-wide project pins (TD-2806).

Pinned workspaces live in the user data dir, not the workspace, so a
clone does not inherit another person's pins.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


def workspace_pins_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "workspace_pins.yaml"


def load_workspace_pins(data_dir: str | Path) -> list[str]:
    """Load pinned workspace paths. Absent or junk is empty."""
    path = workspace_pins_path(data_dir)
    if not path.exists():
        return []
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("paths")
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, str) and p.strip()]


def save_workspace_pins(data_dir: str | Path, paths: list[str]) -> None:
    path = workspace_pins_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".workspace_pins.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump({"paths": paths}, f, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
