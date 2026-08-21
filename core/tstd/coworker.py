"""Machine-wide coworker mode (TD-2902).

When on, closing the window hides it and leaves ``tstd`` running. The
host omits ``--parent-pid`` so a dead window process does not reap the
daemon. Default on once M5 ships. The Settings toggle is TD-2905.

Same shape as skip-all (TD-804): user data dir, YAML bool only.
Absent or unreadable is on — the opposite of skip-all's default.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


def coworker_path(data_dir: str | Path) -> Path:
    """Path of the user-data file that holds the coworker bit."""
    return Path(data_dir) / "coworker.yaml"


def load_coworker(data_dir: str | Path) -> bool:
    """Load coworker mode. Absent, empty, or unreadable is on."""
    path = coworker_path(data_dir)
    if not path.exists():
        return True
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return True
    if data is None:
        return True
    if not isinstance(data, dict):
        return True
    if "enabled" not in data:
        return True
    return data.get("enabled") is True


def save_coworker(data_dir: str | Path, enabled: bool) -> None:
    """Persist coworker mode atomically in the user data dir."""
    path = coworker_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".coworker.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump({"enabled": enabled}, f, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def ensure_coworker(data_dir: str | Path) -> bool:
    """Write the default-on file if it is missing; return the loaded value."""
    if not coworker_path(data_dir).exists():
        save_coworker(data_dir, True)
    return load_coworker(data_dir)
