"""Ask the TST Desk host for its computer-use permission diagnosis (TD-4823).

The Tauri host binds ``{data_dir}/cu-agent.sock`` and is the process TCC
attributes capture and input to. Its ``permissions`` reply carries what no
other probe can know: whether a grant is *stale* (System Settings shows
TST Desk ON, but the row belongs to an older build) and how the host is
signed. ``permissions reset`` runs ``tccutil reset`` there and re-prompts;
only the window sends it — it is never a tool.

Absent socket (Windows, Linux, CLI ``tstd``, tests) → ``None`` and the
daemon falls back to the driver report.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

from ..cu_host import SOCK_ENV, sock_path

TIMEOUT_S = 3.0
# ``tccutil reset`` spawns a process per service.
RESET_TIMEOUT_S = 15.0


def host_socket(data_dir: str | Path) -> Path | None:
    """The host actuator socket, when this platform has one and it exists."""
    if sys.platform != "darwin":
        return None
    env = os.environ.get(SOCK_ENV, "").strip()
    path = Path(env) if env else sock_path(Path(data_dir))
    try:
        return path if path.exists() else None
    except OSError:
        return None


def transact(path: Path, line: str, *, timeout: float = TIMEOUT_S) -> dict[str, Any] | None:
    """Send one text command; parse the JSON line the host answers with."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    buf = b""
    try:
        sock.connect(str(path))
        sock.sendall(line.encode("utf-8") + b"\n")
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    except (OSError, ValueError):
        return None
    finally:
        sock.close()
    head = buf.split(b"\n", 1)[0]
    try:
        parsed: Any = json.loads(head.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if isinstance(parsed, dict) and "screen_recording" in parsed:
        return parsed
    return None


async def host_permissions(data_dir: str | Path, *, reset: bool = False) -> dict[str, Any] | None:
    """The host's permission report, or ``None`` when there is no host socket."""
    path = host_socket(data_dir)
    if path is None:
        return None
    line = "permissions reset" if reset else "permissions"
    timeout = RESET_TIMEOUT_S if reset else TIMEOUT_S
    return await asyncio.to_thread(transact, path, line, timeout=timeout)
