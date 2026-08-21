"""`tst` — a second door to the same daemon (TD-3101, TD-3102, TD-3103).

The window and this CLI share one rendezvous (``port.json``) and one
handshake (``hello`` + the port-file token). This process never binds a
socket. If no live daemon is at the data dir it starts ``tstd`` and
leaves it running; it is not the host, so it does not pass
``--parent-pid``.

``run`` opens a workspace and prints one turn. ``attach`` follows a
session the daemon already owns: replay + live text, detach on SIGINT.
A TTY can answer ``approval_request``; a non-TTY does not hang on stdin.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .cli_approvals import prompt_approval
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


def _add_data_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="daemon data directory (default: the same as tstd / the host)",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse ``tst`` argv. ``run`` and ``attach`` are registered."""
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
    _add_data_dir(run_p)
    attach_p = sub.add_parser(
        "attach",
        help="replay a session from from_seq and stream live events as text",
    )
    attach_p.add_argument("session_id", help="session to follow")
    attach_p.add_argument(
        "--from-seq",
        type=int,
        default=1,
        metavar="N",
        help="replay from this event seq (default: 1)",
    )
    _add_data_dir(attach_p)
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


async def _recv_event(ws: Any, timeout_secs: float | None) -> dict[str, Any]:
    """Next JSON object, skipping application pings (TD-1716)."""
    deadline = None if timeout_secs is None else time.monotonic() + timeout_secs
    while True:
        if deadline is None:
            raw = await ws.recv()
        else:
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
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError
            continue
        return event


def _print_error(event: dict[str, Any]) -> None:
    """Print a daemon ``error`` to stderr, including its typed ``code``."""
    code = event.get("code")
    message = event.get("message")
    if isinstance(code, str) and code:
        if isinstance(message, str) and message and message != code:
            print(f"{code}: {message}", file=sys.stderr)
        else:
            print(code, file=sys.stderr)
        return
    print(str(message or "error"), file=sys.stderr)


def _visible_event_line(event: dict[str, Any]) -> str | None:
    """One-line TTY form of a user-visible event. Not a JSON dump."""
    typ = event.get("type")
    if typ == "turn_complete":
        if event.get("failed"):
            return f"turn failed: {event.get('error_code') or 'turn_failed'}"
        return "turn complete"
    return None


def _finish_assistant_line(printed: bool) -> None:
    if printed:
        sys.stdout.write("\n")
        sys.stdout.flush()


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
            _finish_assistant_line(printed)
            _print_error(event)
            return 1
        if event.get("session_id") != session_id:
            continue
        if typ == "approval_request":
            _finish_assistant_line(printed)
            printed = False
            reply = await prompt_approval(event, session_id)
            if reply is None:
                return 1
            await _send(ws, reply)
            continue
        if typ == "assistant_delta":
            sys.stdout.write(str(event.get("delta") or ""))
            sys.stdout.flush()
            printed = True
        if typ == "turn_complete":
            _finish_assistant_line(printed)
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


def _is_fatal_attach_error(event: dict[str, Any], session_id: str) -> bool:
    """Unknown-session and other connection-scoped errors end the command."""
    if event.get("code") == "session_not_found":
        return True
    sid = event.get("session_id")
    return sid is None or sid != session_id


async def _stream_attached(ws: Any, session_id: str) -> int:
    """Replay + live. Stays on the socket until detach or the daemon drops."""
    printed = False
    try:
        while True:
            event = await _recv_event(ws, None)
            typ = event.get("type")
            if typ == "error":
                if _is_fatal_attach_error(event, session_id):
                    _finish_assistant_line(printed)
                    _print_error(event)
                    return 1
                _print_error(event)
                continue
            if event.get("session_id") != session_id:
                continue
            if typ == "approval_request":
                _finish_assistant_line(printed)
                printed = False
                reply = await prompt_approval(event, session_id)
                if reply is not None:
                    await _send(ws, reply)
                continue
            if typ == "assistant_delta":
                sys.stdout.write(str(event.get("delta") or ""))
                sys.stdout.flush()
                printed = True
                continue
            line = _visible_event_line(event)
            if line is None:
                continue
            _finish_assistant_line(printed)
            printed = False
            print(line)
    except ConnectionClosed:
        _finish_assistant_line(printed)
        return 0


async def _follow_session(ws: Any, session_id: str, from_seq: int) -> int:
    ack = await _recv_event(ws, 10.0)
    if ack.get("type") != "hello_ack":
        if ack.get("type") == "error":
            _print_error(ack)
            return 1
        print("handshake failed", file=sys.stderr)
        return 1

    await _send(ws, {"type": "attach", "session_id": session_id, "from_seq": from_seq})
    try:
        return await _stream_attached(ws, session_id)
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Detach is a viewer unsubscribe (TD-206). Never send cancel.
        with contextlib.suppress(OSError, ConnectionClosed):
            await asyncio.shield(_send(ws, {"type": "detach", "session_id": session_id}))
        raise


async def attach_session(session_id: str, data_dir: Path, from_seq: int = 1) -> int:
    """Connect, attach, replay from ``from_seq``, stream until detach."""
    info = await resolve_daemon(data_dir)
    try:
        async with connect(f"ws://127.0.0.1:{info['port']}") as ws:
            await _send(ws, hello_message(str(info["token"])))
            return await _follow_session(ws, session_id, from_seq)
    except OSError as e:
        raise CliError(f"could not connect to daemon: {e}") from e
    except TimeoutError as e:
        raise CliError("timed out talking to the daemon") from e


def _resolved_data_dir(args: argparse.Namespace) -> Path:
    if args.data_dir is not None:
        return Path(args.data_dir).expanduser().resolve()
    return user_data_dir()


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point. Returns a process exit code."""
    args = parse_args(argv)
    data_dir = _resolved_data_dir(args)
    try:
        if args.command == "run":
            workspace = args.workspace.expanduser().resolve()
            return asyncio.run(run_turn(workspace, args.message, data_dir))
        if args.command == "attach":
            return asyncio.run(attach_session(args.session_id, data_dir, args.from_seq))
        print(f"unknown command: {args.command}", file=sys.stderr)
        return 2
    except CliError as e:
        print(str(e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
