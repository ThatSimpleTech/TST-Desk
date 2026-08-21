"""Machine-wide session stars (TD-3003).

Stars live in the user data dir, not the workspace and not
``sessions.json``, so a clone does not inherit another person's stars
and a parallel title-store change does not collide.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


def session_stars_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "session_stars.yaml"


def load_session_stars(data_dir: str | Path) -> list[str]:
    """Load starred session ids. Absent or junk is empty."""
    path = session_stars_path(data_dir)
    if not path.exists():
        return []
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("ids")
    if not isinstance(raw, list):
        return []
    return [i for i in raw if isinstance(i, str) and i.strip()]


def save_session_stars(data_dir: str | Path, ids: list[str]) -> None:
    path = session_stars_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".session_stars.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump({"ids": ids}, f, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
