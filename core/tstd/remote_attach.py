"""Machine-wide remote-attach switch (TD-3603).

Off by default. The Settings toggle persists here — not the workspace —
and the daemon applies ``remote.bind`` only while the flag is on.
``last_bind`` remembers the Tailscale target (interface or address) so
turning the switch back on does not forget a configured iface. Absent
``last_bind`` is ``tailscale0``, the documented default from TD-3601.

``last_port`` (TD-5005) remembers the port the listener last used while
the switch was on, so a saved phone keeps working across restarts.
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


def _read_fields(path: Path) -> dict[str, Any]:
    """The file's mapping, or empty when absent, unreadable or not a mapping."""
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_fields(path: Path, fields: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".remote-attach.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(fields, f, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def save_remote_attach(
    data_dir: str | Path,
    enabled: bool,
    last_bind: str = DEFAULT_REMOTE_BIND,
) -> None:
    """Persist the switch atomically in the user data dir.

    A remembered ``last_port`` survives the rewrite (TD-5005).
    """
    spec = last_bind.strip() or DEFAULT_REMOTE_BIND
    path = remote_attach_path(data_dir)
    fields: dict[str, Any] = {"enabled": enabled, "last_bind": spec}
    port = load_remote_port(data_dir)
    if port:
        fields["last_port"] = port
    _write_fields(path, fields)


def _valid_port(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 1024 <= value <= 65535:
        return value
    return 0


def load_remote_port(data_dir: str | Path) -> int:
    """The remembered listener port, or 0 when none is usable (TD-5005)."""
    return _valid_port(_read_fields(remote_attach_path(data_dir)).get("last_port"))


def save_remote_port(data_dir: str | Path, port: int) -> None:
    """Remember *port* beside the switch. Out-of-range ports are ignored."""
    if not _valid_port(port):
        return
    path = remote_attach_path(data_dir)
    fields = _read_fields(path)
    if fields.get("last_port") == port:
        return
    fields["last_port"] = port
    _write_fields(path, fields)


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
