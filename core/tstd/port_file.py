"""The daemon's ``port.json``: write it, and remove it only for this process.

A clean shutdown must not delete a file that a successor has already
replaced. The check-then-unlink is not locked. The host does not spawn
the next daemon until this process is dead, so the window is a crash
racing a hand-started second process — the stale-file path at the next
start, not a reason to take a lock on the quit path (TD-4848).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .logging import get_logger

# The line operators already grep is ``tstd.ws``. The write moved here
# so the server module could stay the listener; the logger name did not.
log = get_logger("tstd.ws")

PORT_FILE_NAME = "port.json"


def port_file_path(data_dir: Path) -> Path:
    """Path of ``port.json`` inside a daemon data directory."""
    return data_dir / PORT_FILE_NAME


def _as_pid(value: object) -> int | None:
    """A process id, or None when the value is not one.

    ``bool`` is an ``int`` subclass. ``True`` must not match pid 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def read_port_pid(data_dir: Path) -> int | None:
    """The pid named in the port file, or None when it is not an int.

    Missing, unreadable, and non-object files are None. The token is
    never returned and never logged.
    """
    path = port_file_path(data_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return _as_pid(data.get("pid"))


def release_port_file(data_dir: Path, *, pid: int | None = None) -> bool:
    """Delete the port file when it still names *pid*.

    *pid* defaults to this process. Returns True only when this call
    unlinked the file. A foreign pid, a corrupt file, and a second
    call are False — the file is left as it was.
    """
    owner = _as_pid(os.getpid() if pid is None else pid)
    if owner is None or read_port_pid(data_dir) != owner:
        return False
    try:
        port_file_path(data_dir).unlink()
    except OSError:
        return False
    return True


def write_port_file(data_dir: Path, port: int, token: str, pid: int | None = None) -> Path:
    """Write the port, token, and PID with restricted permissions.

    A file already at the path is a previous daemon (or a crash). It is
    replaced so a dead port file cannot block startup. That leftover is
    expected, so the log is INFO and names the dead pid. The token is
    not logged.

    The write is atomic (temp file + rename) so a supervising host
    polling the file never sees a partial read.

    Args:
        data_dir: The daemon's user data directory.
        port: The port the WebSocket server is listening on.
        token: The auth token clients must present.
        pid: The daemon's process ID. Defaults to the current process.

    Returns:
        The path to the written port file.
    """
    port_file = port_file_path(data_dir)
    if port_file.exists():
        log.info(
            "stale port file detected, replacing",
            extra={
                "extra_fields": {
                    "path": str(port_file),
                    "pid": read_port_pid(data_dir),
                }
            },
        )
    content = json.dumps(
        {"port": port, "token": token, "pid": pid if pid is not None else os.getpid()},
        indent=2,
    )
    tmp_file = data_dir / f".{PORT_FILE_NAME}.{os.getpid()}.tmp"
    tmp_file.write_text(content)
    # Set mode 0o600 (owner read/write only) before it is renamed into place.
    # POSIX mode bits only: on Windows os.chmod can merely toggle the
    # read-only flag, so this is a silent no-op there — ACL-based
    # restriction is a separate hardening story (TD-1406).
    tmp_file.chmod(0o600)
    os.replace(tmp_file, port_file)
    log.info(
        "port file written",
        extra={"extra_fields": {"path": str(port_file), "port": port}},
    )
    return port_file
