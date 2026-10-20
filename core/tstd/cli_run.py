"""``tst run``: one turn through the window's own daemon (TD-3101, TD-5111).

By default the turn opens a new session in ``--workspace``. ``--session
<id>`` sends the message into that session instead, and ``--continue``
into the workspace's most recently used session that can still run
turns, the one the window's quick entry would open. The message comes
from ``--message``, or from stdin when ``--message`` is ``-`` or left
out. ``--output`` picks plain text, one JSON result, or the session's
protocol events as NDJSON. The exit code says how the turn ended (see
:mod:`tstd.cli_turn`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .cli import (
    CliError,
    _positive_seconds,
    _print_error,
    _recv_event,
    _send,
    hello_message,
    resolve_daemon,
)
from .cli_turn import EXIT_USAGE, OUTPUTS, Output, TurnOutcome, stream_turn

_REPLY_SECS = 15.0

#: States a session never leaves: the daemon refuses their messages with
#: ``session_not_running``. The same set as ``tstd.session.TERMINAL_STATES``
#: (a test holds them equal), kept here so the CLI need not import the loop.
ENDED_STATES = frozenset({"complete", "failed", "cancelled", "interrupted"})


class UsageError(Exception):
    """A command line that cannot run. Exits ``EXIT_USAGE``."""


class RunError(Exception):
    """The run stopped before its turn began, with a typed code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def add_run_command(
    sub: Any, add_data_dir: Callable[[argparse.ArgumentParser], None]
) -> argparse.ArgumentParser:
    """Register ``run`` on the ``tst`` parser and return its subparser."""
    run_p: argparse.ArgumentParser = sub.add_parser(
        "run",
        help="send one message to a new or existing session and print the reply",
    )
    run_p.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="workspace directory (required unless --session is given)",
    )
    run_p.add_argument(
        "--message",
        default=None,
        help="user message for this turn; '-' or leaving it out reads stdin",
    )
    target = run_p.add_mutually_exclusive_group()
    target.add_argument(
        "--session",
        default=None,
        metavar="ID",
        help="send the message into this existing session",
    )
    target.add_argument(
        "--continue",
        dest="continue_last",
        action="store_true",
        help="send the message into the workspace's most recently used session",
    )
    run_p.add_argument(
        "--output",
        choices=OUTPUTS,
        default="text",
        help="text (default), json (one result object) or ndjson (protocol events)",
    )
    run_p.add_argument(
        "--timeout",
        type=_positive_seconds,
        default=None,
        metavar="SECONDS",
        help=(
            "base seconds to wait for the turn "
            "(default: scheduler.max_run_seconds); provider retries are added"
        ),
    )
    add_data_dir(run_p)
    return run_p


def check_run_args(run_p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Usage errors argparse cannot express on its own."""
    if args.workspace is None and args.session is None:
        run_p.error("--workspace is required unless --session is given")


def read_message(message: str | None, stdin: TextIO | None) -> str:
    """``--message``, or all of stdin for ``-`` or no ``--message``.

    A terminal is only read when ``-`` asks for it: a script that forgot
    ``--message`` must not sit waiting on a keyboard. One trailing line
    break from ``echo`` or a file is dropped.
    """
    if message is not None and message != "-":
        return message
    if stdin is None:
        raise UsageError("pass --message; there is no stdin to read")
    if message is None and _isatty(stdin):
        raise UsageError("pass --message, or pipe the message on stdin")
    text = stdin.read()
    if text.endswith("\n"):
        text = text[:-1]
    if text.endswith("\r"):
        text = text[:-1]
    if not text.strip():
        raise UsageError("the message on stdin is empty")
    return text


def _isatty(stream: Any) -> bool:
    check = getattr(stream, "isatty", None)
    return bool(check()) if callable(check) else False


def _when(row: dict[str, Any], key: str) -> datetime:
    try:
        when = datetime.fromisoformat(str(row.get(key)))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _same_dir(recorded: str, workspace: Path) -> bool:
    return Path(recorded).expanduser().resolve() == workspace


def pick_session(
    rows: list[dict[str, Any]],
    *,
    session_id: str | None,
    workspace: Path | None,
) -> dict[str, Any]:
    """The ``session_list`` row this run sends into.

    ``session_id`` names it; otherwise it is the local, unarchived session
    of ``workspace`` that can still run turns and was used last (the
    daemon moves ``updated_at`` when a turn starts), as the window's quick
    entry picks. A session with a turn in flight is refused rather than
    queued behind: that turn may be waiting on a person. Blocking (it
    resolves paths); call it off the event loop.
    """
    if session_id is not None:
        row = next((r for r in rows if r.get("session_id") == session_id), None)
        if row is None:
            raise RunError("session_not_found", f"Session {session_id!r} not found")
        if workspace is not None and not _same_dir(str(row.get("workspace_path")), workspace):
            raise RunError(
                "workspace_mismatch",
                f"Session {session_id} is in {row.get('workspace_path')}, not {workspace}",
            )
    else:
        assert workspace is not None
        mine = [
            r
            for r in rows
            if r.get("machine") is None
            and not r.get("archived")
            and r.get("state") not in ENDED_STATES
            and _same_dir(str(r.get("workspace_path")), workspace)
        ]
        if not mine:
            raise RunError(
                "no_session_to_continue",
                f"No session to continue in {workspace}; run without --continue to start one",
            )
        row = max(mine, key=lambda r: (_when(r, "updated_at"), _when(r, "created_at")))
    if row.get("busy"):
        raise RunError(
            "session_busy",
            f"Session {row.get('session_id')} is running a turn. Let it finish, or answer "
            "its approval in the TST Desk window, then send again.",
        )
    return row


async def _reply_of(ws: Any, wanted: str) -> dict[str, Any]:
    """The next event of type *wanted*; a daemon ``error`` stops the run."""
    deadline = time.monotonic() + _REPLY_SECS
    while time.monotonic() < deadline:
        event = await _recv_event(ws, max(0.1, deadline - time.monotonic()))
        if event.get("type") == wanted:
            return event
        if event.get("type") == "error":
            code = str(event.get("code") or "error")
            raise RunError(code, str(event.get("message") or code))
    raise TimeoutError


async def _open_new(ws: Any, workspace: Path) -> str:
    await _send(ws, {"type": "open_workspace", "path": str(workspace)})
    opened = await _reply_of(ws, "session_state")
    if not opened.get("session_id"):
        raise RunError("no_session", "open_workspace did not return a session")
    session_id = str(opened["session_id"])
    await _send(ws, {"type": "attach", "session_id": session_id, "from_seq": 1})
    return session_id


async def _open_existing(ws: Any, session_id: str | None, workspace: Path | None) -> str:
    """Choose the session and attach after its last event, so no old turn replays."""
    await _send(ws, {"type": "list_sessions"})
    listed = await _reply_of(ws, "session_list")
    rows = [r for r in listed.get("sessions") or [] if isinstance(r, dict)]
    row = await asyncio.to_thread(pick_session, rows, session_id=session_id, workspace=workspace)
    chosen = str(row["session_id"])
    from_seq = int(row.get("event_count") or 0) + 1
    await _send(ws, {"type": "attach", "session_id": chosen, "from_seq": from_seq})
    return chosen


async def _turn(
    ws: Any,
    workspace: Path | None,
    message: str,
    data_dir: Path,
    budget: float | None,
    session_id: str | None,
    continue_last: bool,
    output: Output,
) -> TurnOutcome:
    ack = await _recv_event(ws, 10.0)
    if ack.get("type") != "hello_ack":
        if ack.get("type") == "error":
            code = str(ack.get("code") or "error")
            raise RunError(code, str(ack.get("message") or code))
        raise RunError("handshake_failed", "handshake failed")
    existing = session_id is not None or continue_last
    if existing:
        sid = await _open_existing(ws, session_id, workspace)
    else:
        assert workspace is not None
        sid = await _open_new(ws, workspace)
    await _send(ws, {"type": "user_message", "session_id": sid, "content": message})
    return await stream_turn(
        ws, sid, data_dir, budget, output=output, content=message if existing else None
    )


async def _outcome(
    workspace: Path | None,
    message: str,
    data_dir: Path,
    budget: float | None,
    session_id: str | None,
    continue_last: bool,
    output: Output,
) -> TurnOutcome:
    info = await resolve_daemon(data_dir)
    try:
        async with connect(f"ws://127.0.0.1:{info['port']}") as ws:
            await _send(ws, hello_message(str(info["token"])))
            return await _turn(
                ws, workspace, message, data_dir, budget, session_id, continue_last, output
            )
    except RunError as e:
        _print_error({"code": e.code, "message": e.message})
        return TurnOutcome(
            status="error", session_id=session_id, error_code=e.code, error_message=e.message
        )
    except OSError as e:
        raise CliError(f"could not connect to daemon: {e}") from e
    except TimeoutError as e:
        raise CliError("timed out talking to the daemon") from e
    except ConnectionClosed as e:
        raise CliError("the daemon closed the connection") from e


async def run_turn(
    workspace: Path | None,
    message: str,
    data_dir: Path,
    *,
    budget: float | None = None,
    session_id: str | None = None,
    continue_last: bool = False,
    output: Output = "text",
) -> int:
    """Run one turn and print it in *output* form. Returns the exit code.

    In ``json`` mode a failure to reach the daemon is still one object, so
    a script always has something to parse; the other modes leave that
    message to ``main``.
    """
    try:
        outcome = await _outcome(
            workspace, message, data_dir, budget, session_id, continue_last, output
        )
    except CliError as e:
        if output != "json":
            raise
        print(str(e), file=sys.stderr)
        outcome = TurnOutcome(
            status="error",
            session_id=session_id,
            error_code="connection_error",
            error_message=str(e),
        )
    if output == "json":
        sys.stdout.write(json.dumps(outcome.as_json()) + "\n")
        sys.stdout.flush()
    return outcome.exit_code


def run_command(args: argparse.Namespace, data_dir: Path) -> int:
    """``tst run`` after parsing: read the message, then run the turn."""
    try:
        message = read_message(args.message, sys.stdin)
    except UsageError as e:
        print(f"tst run: {e}", file=sys.stderr)
        return EXIT_USAGE
    workspace = None if args.workspace is None else args.workspace.expanduser().resolve()
    return asyncio.run(
        run_turn(
            workspace,
            message,
            data_dir,
            budget=args.timeout,
            session_id=args.session,
            continue_last=args.continue_last,
            output=args.output,
        )
    )
