"""Machine-wide memory preferences (TD-2603).

The global-memory toggle is not a workspace file: a `.tst/config.yaml`
bit would be committed and surprise the next clone. Same shape as
skip-all (TD-804): user data dir, YAML true only, absent is off.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .logging import get_logger

log = get_logger("tstd.memory_pref")


def load_global_path(data_dir: str | Path) -> Path:
    """Path of the user-data file that holds the global-memory bit."""
    return Path(data_dir) / "memory.yaml"


def load_global_memory(data_dir: str | Path) -> bool:
    """Load the toggle. Absent or unreadable is off."""
    path = load_global_path(data_dir)
    if not path.exists():
        return False
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(data, dict):
        return False
    return data.get("load_global") is True


def save_global_memory(data_dir: str | Path, enabled: bool) -> None:
    """Persist the toggle atomically in the user data dir."""
    path = load_global_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".memory.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump({"load_global": enabled}, f, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
