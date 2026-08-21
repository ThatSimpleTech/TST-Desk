"""Rotating user-data-dir token for non-loopback hellos (TD-3602).

Loopback keeps the port-file token. A connection on the extra Tailscale
listener — or any non-loopback peer — must present this token. The
port-file token is not enough there. The file is never the port.json
token and the value is never logged.
"""

from __future__ import annotations

import os
from pathlib import Path

from .logging import get_logger

log = get_logger("tstd.remote_auth")

REMOTE_TOKEN_FILE = "remote-token"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def write_remote_token_file(data_dir: Path, token: str) -> Path:
    """Write the remote-auth token with restricted permissions.

    Atomic temp + rename, chmod ``0o600``. The token itself is not logged.
    """
    path = data_dir / REMOTE_TOKEN_FILE
    tmp_file = data_dir / f".{REMOTE_TOKEN_FILE}.{os.getpid()}.tmp"
    tmp_file.write_text(token, encoding="utf-8")
    tmp_file.chmod(0o600)
    os.replace(tmp_file, path)
    log.info(
        "remote token file written",
        extra={"extra_fields": {"path": str(path)}},
    )
    return path


def remove_remote_token_file(data_dir: Path) -> None:
    """Delete the remote-token file, if present. Used on clean shutdown."""
    (data_dir / REMOTE_TOKEN_FILE).unlink(missing_ok=True)


def read_remote_token_file(data_dir: Path) -> str | None:
    """Read ``remote-token`` if it exists. Returns None when missing."""
    path = data_dir / REMOTE_TOKEN_FILE
    if not path.exists():
        return None
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def addr_host(addr: object) -> str:
    """Host string from a socket address (tuple, str, or None)."""
    if addr is None:
        return ""
    if isinstance(addr, tuple) and addr:
        host = addr[0]
        return host if isinstance(host, str) else str(host)
    if isinstance(addr, str):
        return addr
    return ""


def is_loopback_host(host: str) -> bool:
    if host in _LOOPBACK_HOSTS:
        return True
    # IPv4-mapped loopback still counts as loopback.
    return host.startswith("::ffff:") and host.removeprefix("::ffff:") in {"127.0.0.1"}


def connection_is_remote(
    extra_host: str | None,
    local_address: object,
    remote_address: object,
) -> bool:
    """True when this socket is the extra (Tailscale) listener or a non-loopback peer.

    A ``127.0.0.1`` extra host is not remote — that listener is still loopback.
    """
    extra_is_remote = extra_host is not None and not is_loopback_host(extra_host)
    local_host = addr_host(local_address)
    remote_host = addr_host(remote_address)
    if extra_is_remote and local_host == extra_host:
        return True
    return bool(remote_host) and not is_loopback_host(remote_host)
