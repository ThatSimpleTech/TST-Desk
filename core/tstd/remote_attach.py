"""Machine-wide remote-attach switch (TD-3603).

Off by default. The Settings toggle persists here — not the workspace —
and the daemon applies ``remote.bind`` only while the flag is on.
``last_bind`` remembers the Tailscale target (interface or address) so
turning the switch back on does not forget a configured iface. Absent
``last_bind`` is ``tailscale0``, the documented default from TD-3601.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

DEFAULT_REMOTE_BIND = "tailscale0"


def remote_attach_path(data_dir: str | Path) -> Path:
    """Path of the user-data file that holds the remote-attach switch."""
    return Path(data_dir) / "remote-attach.yaml"


def load_remote_attach(data_dir: str | Path) -> tuple[bool, str]:
    """Load ``(enabled, last_bind)``. Absent or unreadable is off."""
    path = remote_attach_path(data_dir)
    if not path.exists():
        return False, DEFAULT_REMOTE_BIND
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False, DEFAULT_REMOTE_BIND
    if not isinstance(data, dict):
        return False, DEFAULT_REMOTE_BIND
    enabled = data.get("enabled") is True
    raw = data.get("last_bind", DEFAULT_REMOTE_BIND)
    last_bind = raw.strip() if isinstance(raw, str) and raw.strip() else DEFAULT_REMOTE_BIND
    return enabled, last_bind


def save_remote_attach(
    data_dir: str | Path,
    enabled: bool,
    last_bind: str = DEFAULT_REMOTE_BIND,
) -> None:
    """Persist the switch atomically in the user data dir."""
    spec = last_bind.strip() or DEFAULT_REMOTE_BIND
    path = remote_attach_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".remote-attach.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump({"enabled": enabled, "last_bind": spec}, f, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def bind_spec_when_enabled(last_bind: str, configured: str) -> str:
    """Target used when the switch turns on.

    Last-known wins, then a non-empty ``remote.bind`` from config, then
    ``tailscale0``.
    """
    for candidate in (last_bind, configured):
        stripped = candidate.strip()
        if stripped:
            return stripped
    return DEFAULT_REMOTE_BIND
