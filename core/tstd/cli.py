"""`tst` — a second door to the same daemon (TD-3101).

The window and this CLI share one rendezvous (``port.json``) and one
handshake (``hello`` + the port-file token). This process never binds a
socket. If no live daemon is at the data dir it starts ``tstd`` and
leaves it running; it is not the host, so it does not pass
``--parent-pid``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from .logging import user_data_dir
from .protocol import PROTOCOL_VERSION
from .ws import read_port_file

# A healthy mock turn finishes in milliseconds; a live one should not
# sit here forever. Long enough for a slow first token, short enough
# that a hung provider fails the command.
_TURN_TIMEOUT_SECS = 120.0
_PORT_FILE_TIMEOUT_SECS = 15.0


class CliError(Exception):
    """User-visible failure. The message is printed to stderr."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse ``tst`` argv. Only ``run`` is registered this story."""
    parser = argparse.ArgumentParser(
        prog="tst",
        description="TST Desk CLI — same daemon as the window, different door.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser(
        "run",
        help="open a workspace, send one message, print the assistant reply",
    )
    run_p.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="workspace directory to open",
    )
    run_p.add_argument(
        "--message",
        required=True,
        help="user message for this turn",
    )
    run_p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="daemon data directory (default: the same as tstd / the host)",
    )
    return parser.parse_args(argv)


def hello_message(token: str) -> dict[str, Any]:
    """The same ``hello`` the window sends."""
    return {"type": "hello", "token": token, "version": PROTOCOL_VERSION}


def daemon_argv(data_dir: Path) -> list[str]:
    """Argv that starts the daemon without claiming to be the host.

    ``python -m tstd.daemon`` so the port-file pid is this interpreter,
    not a console-script trampoline (TD-1406). No ``--parent-pid``.
    """
    return [sys.executable, "-m", "tstd.daemon", "--data-dir", str(data_dir)]


def spawn_daemon(data_dir: Path) -> None:
    """Start ``tstd`` and return immediately. The daemon outlives this CLI."""
    data_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        daemon_argv(data_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _pid_is_alive(pid: int) -> bool:
    """Port-file liveness. Windows cannot use ``os.kill(pid, 0)``."""
    if pid <= 0:
        return False
    if os.name == "nt":
        from .daemon import _parent_alive

        return _parent_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_port_info(data_dir: Path) -> dict[str, Any] | None:
    """Return the port file only when the pid is live and the token is set."""
    info = read_port_file(data_dir)
    if info is None:
        return None
    token = info.get("token")
    port = info.get("port")
    pid = info.get("pid")
    if not isinstance(token, str) or not token:
        return None
    if not isinstance(port, int) or port <= 0:
        return None
    if not isinstance(pid, int) or not _pid_is_alive(pid):
        return None
    return info


async def wait_for_live_port_file(
    data_dir: Path, timeout_secs: float = _PORT_FILE_TIMEOUT_SECS
) -> dict[str, Any]:
    """Poll until a live daemon has written the port file."""
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        info = await asyncio.to_thread(live_port_info, data_dir)
        if info is not None:
            return info
        await asyncio.sleep(0.05)
    raise CliError(f"daemon did not write a live port file under {data_dir}")


async def resolve_daemon(data_dir: Path) -> dict[str, Any]:
    """Attach to a running daemon, or start one and wait for the port file."""
    info = await asyncio.to_thread(live_port_info, data_dir)
    if info is not None:
        return info
    spawn_daemon(data_dir)
    return await wait_for_live_port_file(data_dir)


async def _send(ws: Any, message: dict[str, Any]) -> None:
    await ws.send(json.dumps(message))


async def _recv_event(ws: Any, timeout_secs: float) -> dict[str, Any]:
    """Next JSON object, skipping application pings (TD-1716)."""
    deadline = time.monotonic() + timeout_secs
    while True:
        remaining = max(0.1, deadline - time.monotonic())
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not isinstance(raw, str):
            raise CliError("daemon sent a non-text frame")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CliError("daemon sent invalid JSON") from e
        if not isinstance(event, dict):
            raise CliError("daemon sent a non-object frame")
        if event.get("type") == "ping":
            if time.monotonic() >= deadline:
                raise TimeoutError
            continue
        return event


def _print_error(event: dict[str, Any]) -> None:
    message = event.get("message") or event.get("code") or "error"
    print(str(message), file=sys.stderr)


async def _run_session(ws: Any, workspace: Path, message: str) -> int:
    ack = await _recv_event(ws, 10.0)
    if ack.get("type") != "hello_ack":
        if ack.get("type") == "error":
            _print_error(ack)
            return 1
        print("handshake failed", file=sys.stderr)
        return 1

    await _send(ws, {"type": "open_workspace", "path": str(workspace)})
    opened = await _recv_event(ws, 15.0)
    if opened.get("type") == "error":
        _print_error(opened)
        return 1
    if opened.get("type") != "session_state" or not opened.get("session_id"):
        print("open_workspace did not return a session", file=sys.stderr)
        return 1
    session_id = str(opened["session_id"])

    await _send(ws, {"type": "attach", "session_id": session_id, "from_seq": 1})
    await _send(
        ws,
        {"type": "user_message", "session_id": session_id, "content": message},
    )
    return await _stream_turn(ws, session_id)


async def _stream_turn(ws: Any, session_id: str) -> int:
    printed = False
    deadline = time.monotonic() + _TURN_TIMEOUT_SECS
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            event = await _recv_event(ws, remaining)
        except TimeoutError:
            break
        typ = event.get("type")
        if typ == "error":
            if printed:
                sys.stdout.write("\n")
                sys.stdout.flush()
            _print_error(event)
            return 1
        if event.get("session_id") != session_id:
            continue
        if typ == "assistant_delta":
            sys.stdout.write(str(event.get("delta") or ""))
            sys.stdout.flush()
            printed = True
        if typ == "turn_complete":
            if printed:
                sys.stdout.write("\n")
                sys.stdout.flush()
            if event.get("failed"):
                print(str(event.get("error_code") or "turn_failed"), file=sys.stderr)
                return 1
            return 0
    print("timed out waiting for turn_complete", file=sys.stderr)
    return 1


async def run_turn(workspace: Path, message: str, data_dir: Path) -> int:
    """Connect, open the workspace, send one turn, print the reply."""
    info = await resolve_daemon(data_dir)
    try:
        async with connect(f"ws://127.0.0.1:{info['port']}") as ws:
            await _send(ws, hello_message(str(info["token"])))
            return await _run_session(ws, workspace, message)
    except OSError as e:
        raise CliError(f"could not connect to daemon: {e}") from e
    except TimeoutError as e:
        raise CliError("timed out talking to the daemon") from e


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point. Returns a process exit code."""
    args = parse_args(argv)
    if args.command != "run":
        print(f"unknown command: {args.command}", file=sys.stderr)
        return 2
    workspace = args.workspace.expanduser().resolve()
    data_dir = (
        args.data_dir.expanduser().resolve() if args.data_dir is not None else user_data_dir()
    )
    try:
        return asyncio.run(run_turn(workspace, args.message, data_dir))
    except CliError as e:
        print(str(e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
