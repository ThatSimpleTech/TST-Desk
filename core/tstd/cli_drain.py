"""`tst drain` (TD-5023): let open turns finish before a restart.

Talks to the running daemon over loopback with the port-file token, and
never starts one: with no daemon there is nothing to drain, and one
started here would be the process the deploy is about to replace. The
daemon refuses new turns while it waits up to ``--timeout`` seconds for
the open ones, and for ``--hold`` seconds after; the hold ends by itself.
``--release`` lifts it at once.

Exit 0 when no turn is still running, 2 when one is, 1 when no daemon
is running. A turn paused for the person (the progress guard or a cap)
or parked on an approval never finishes by itself, so it is listed apart
and does not hold a deploy up: the restart seals it, and Continue picks
it up (TD-5018). ``--strict`` exits 2 for those too (TD-5155). Only
session ids are printed, never a title or a message.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .cli import CliError, _print_error, _recv_event, _send, hello_message, live_port_info

DRAIN_LIMIT_SECS = 600
# The daemon answers when its wait ends; this is only the CLI's patience
# past that.
_REPLY_GRACE_SECS = 15.0


def add_drain_command(sub: Any, add_data_dir: Callable[[argparse.ArgumentParser], None]) -> None:
    """Register ``drain`` on the ``tst`` parser."""
    drain_p = sub.add_parser(
        "drain", help="let open turns finish and hold new ones, before a restart"
    )
    drain_p.add_argument(
        "--timeout",
        type=int,
        default=30,
        metavar="N",
        help="seconds to wait for open turns (0-600, default: 30)",
    )
    drain_p.add_argument(
        "--hold",
        type=int,
        default=120,
        metavar="N",
        help="seconds to keep refusing new turns after the wait (0-600, default: 120)",
    )
    drain_p.add_argument("--release", action="store_true", help="lift a hold now")
    drain_p.add_argument(
        "--strict",
        action="store_true",
        help="exit 2 for a turn paused or waiting for approval too, not only a running one",
    )
    drain_p.add_argument("--json", action="store_true", help="print the daemon's drain_result")
    add_data_dir(drain_p)


def drain_frame(timeout: int, hold: int, release: bool) -> dict[str, Any]:
    """The ``drain`` frame. Raises CliError for a value the daemon would refuse."""
    if release:
        return {"type": "drain", "timeout_s": 0, "hold_s": 0}
    for flag, value in (("--timeout", timeout), ("--hold", hold)):
        if not 0 <= value <= DRAIN_LIMIT_SECS:
            raise CliError(f"{flag} must be between 0 and {DRAIN_LIMIT_SECS} seconds")
    return {"type": "drain", "timeout_s": timeout, "hold_s": hold}


def _ids(result: dict[str, Any], key: str) -> list[str]:
    raw = result.get(key)
    return [str(sid) for sid in raw] if isinstance(raw, list) else []


_PAUSED = "paused, waiting on the user"
_APPROVAL = "waiting for approval"


def _waiting_on_user(result: dict[str, Any]) -> list[tuple[str, str]]:
    """Turns that only the person can move on, each with how it waits."""
    return [(sid, _PAUSED) for sid in _ids(result, "paused")] + [
        (sid, _APPROVAL) for sid in _ids(result, "awaiting_approval")
    ]


def exit_code(result: dict[str, Any], *, strict: bool) -> int:
    """0 when the restart may go ahead, else 2 (TD-5155).

    A daemon from before TD-5155 lists a paused turn as running, so it
    still exits 2 here.
    """
    if strict:
        return 0 if result.get("drained") is True else 2
    return 2 if _ids(result, "running") else 0


def _report(result: dict[str, Any], *, release: bool, strict: bool) -> None:
    if release:
        print("Released. New turns are taken again.")
        return
    waiting = _waiting_on_user(result)
    if exit_code(result, strict=strict) == 0:
        until = result.get("hold_until")
        print(f"Drained. New turns are held until {until}." if until else "Drained.")
        if waiting:
            print("Waiting on the user (a restart seals these; Continue picks them up):")
            for sid, how in waiting:
                print(f"  {sid}  {how}")
        return
    print("Still open:")
    for sid in _ids(result, "running"):
        print(f"  {sid}  running")
    for sid, how in waiting:
        print(f"  {sid}  {how}")


async def _ask_daemon(info: dict[str, Any], frame: dict[str, Any]) -> dict[str, Any] | None:
    """The daemon's ``drain_result``, or None after printing its error."""
    try:
        async with connect(f"ws://127.0.0.1:{info['port']}") as ws:
            await _send(ws, hello_message(str(info["token"])))
            ack = await _recv_event(ws, 10.0)
            if ack.get("type") != "hello_ack":
                if ack.get("type") == "error":
                    _print_error(ack)
                else:
                    print("handshake failed", file=sys.stderr)
                return None
            await _send(ws, frame)
            wait = float(frame["timeout_s"]) + _REPLY_GRACE_SECS
            while True:
                event = await _recv_event(ws, wait)
                if event.get("type") == "drain_result":
                    return event
                if event.get("type") == "error":
                    _print_error(event)
                    return None
    except OSError as e:
        raise CliError(f"could not connect to daemon: {e}") from e
    except TimeoutError as e:
        raise CliError("timed out waiting for the daemon to drain") from e
    except ConnectionClosed as e:
        raise CliError("the daemon closed the connection") from e


async def drain(
    data_dir: Path, frame: dict[str, Any], *, as_json: bool, strict: bool = False
) -> int:
    """``tst drain``. Never starts a daemon."""
    info = await asyncio.to_thread(live_port_info, data_dir)
    if info is None:
        print(f"No TST Desk daemon is running for {data_dir}.", file=sys.stderr)
        return 1
    result = await _ask_daemon(info, frame)
    if result is None:
        return 1
    release = frame["timeout_s"] == 0 and frame["hold_s"] == 0
    if as_json:
        print(json.dumps(result))
    else:
        _report(result, release=release, strict=strict)
    if release:
        return 0
    return exit_code(result, strict=strict)


def run_drain_command(args: argparse.Namespace, data_dir: Path) -> int:
    """Dispatch ``drain`` from :func:`tstd.cli.main`."""
    frame = drain_frame(args.timeout, args.hold, args.release)
    return asyncio.run(drain(data_dir, frame, as_json=args.json, strict=args.strict))
