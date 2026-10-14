"""`tst pair` and `tst devices` (TD-5014).

Both talk to the running daemon over loopback with the port-file token,
exactly as ``attach`` does: pairing and removal are loopback-only verbs.
``pair`` prints the code, the link and a terminal QR, then waits for the
phone (Ctrl-C cancels the code). The code itself is never written to a log.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .cli import CliError, _print_error, _recv_event, _send, hello_message, resolve_daemon
from .remote_devices import CODE_TTL_SECONDS, write_terminal_qr

# The daemon expires the code on its own; a little slack covers clock skew
# between this wait and the daemon's.
_PAIR_WAIT_SECS = CODE_TTL_SECONDS + 5.0


def add_pairing_commands(sub: Any, add_data_dir: Callable[[argparse.ArgumentParser], None]) -> None:
    """Register ``pair`` and ``devices [remove <id>]`` on the ``tst`` parser."""
    pair_p = sub.add_parser("pair", help="show a code and QR to pair a phone with this computer")
    pair_p.add_argument(
        "--no-wait",
        action="store_true",
        help="print the code and exit instead of waiting for the phone",
    )
    add_data_dir(pair_p)
    devices_p = sub.add_parser("devices", help="list paired devices, or remove one")
    add_data_dir(devices_p)
    devices_sub = devices_p.add_subparsers(dest="devices_command")
    remove_p = devices_sub.add_parser("remove", help="forget a paired device")
    remove_p.add_argument("device_id", help="device id from `tst devices`")
    # SUPPRESS: a default here would overwrite `tst devices --data-dir X remove`.
    remove_p.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS)


async def _with_daemon(data_dir: Path, body: Callable[[Any], Awaitable[int]]) -> int:
    """Connect, hello, run *body* on the live socket."""
    info = await resolve_daemon(data_dir)
    try:
        async with connect(f"ws://127.0.0.1:{info['port']}") as ws:
            await _send(ws, hello_message(str(info["token"])))
            ack = await _recv_event(ws, 10.0)
            if ack.get("type") != "hello_ack":
                if ack.get("type") == "error":
                    _print_error(ack)
                else:
                    print("handshake failed", file=sys.stderr)
                return 1
            return await body(ws)
    except OSError as e:
        raise CliError(f"could not connect to daemon: {e}") from e
    except TimeoutError as e:
        raise CliError("timed out talking to the daemon") from e
    except ConnectionClosed as e:
        raise CliError("the daemon closed the connection") from e


def _print_pairing_code(event: dict[str, Any]) -> None:
    link = str(event.get("link") or "")
    print(f"Pair a phone with {event.get('desktop_name')}")
    print(f"Address: {event.get('host')}:{event.get('port')}")
    print(f"Code:    {event.get('display_code')}")
    print(f"Link:    {link}")
    print()
    try:
        write_terminal_qr(link, sys.stdout)
    except UnicodeEncodeError:
        # A legacy console code page cannot draw the blocks; the code works.
        print("(This terminal cannot draw the QR. Type the code on the phone.)")
    print()
    print("The code expires in 10 minutes.")
    sys.stdout.flush()


async def _wait_for_phone(ws: Any) -> int:
    deadline = time.monotonic() + _PAIR_WAIT_SECS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("The code expired. Run `tst pair` again.", file=sys.stderr)
            return 1
        try:
            event = await _recv_event(ws, remaining)
        except TimeoutError:
            continue
        if event.get("type") == "pairing_completed":
            device = event.get("device")
            name = device.get("name") if isinstance(device, dict) else None
            print(f"Paired: {name or 'a device'}")
            return 0
        if event.get("type") == "error":
            _print_error(event)
            return 1


async def _pair(ws: Any, wait: bool) -> int:
    await _send(ws, {"type": "start_pairing"})
    reply = await _recv_event(ws, 15.0)
    if reply.get("type") == "error":
        _print_error(reply)
        return 1
    if reply.get("type") != "pairing_code":
        print("start_pairing did not return a code", file=sys.stderr)
        return 1
    _print_pairing_code(reply)
    if not wait:
        return 0
    print("Waiting for the phone... (Ctrl-C cancels the code)")
    try:
        return await _wait_for_phone(ws)
    except (asyncio.CancelledError, KeyboardInterrupt):
        with contextlib.suppress(OSError, ConnectionClosed):
            await asyncio.shield(_send(ws, {"type": "cancel_pairing"}))
        raise


async def pair_device(data_dir: Path, wait: bool = True) -> int:
    """``tst pair``: show a code, then wait for ``pairing_completed``."""
    return await _with_daemon(data_dir, lambda ws: _pair(ws, wait))


async def _setup_state(ws: Any) -> dict[str, Any] | None:
    await _send(ws, {"type": "get_setup_state"})
    reply = await _recv_event(ws, 15.0)
    if reply.get("type") == "setup_state":
        return reply
    if reply.get("type") == "error":
        _print_error(reply)
    else:
        print("get_setup_state did not return setup_state", file=sys.stderr)
    return None


def _devices_of(state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("remote_devices")
    return [d for d in raw if isinstance(d, dict)] if isinstance(raw, list) else []


async def _list(ws: Any) -> int:
    state = await _setup_state(ws)
    if state is None:
        return 1
    devices = _devices_of(state)
    if not devices:
        print("No paired devices. Run `tst pair` to add one.")
        return 0
    for d in devices:
        status = "connected" if d.get("connected") else f"last seen {d.get('last_seen') or 'never'}"
        print(f"{d.get('id')}  {d.get('name')}  paired {d.get('paired_at')}  {status}")
    return 0


async def _remove(ws: Any, device_id: str) -> int:
    before = await _setup_state(ws)
    if before is None:
        return 1
    known = {str(d.get("id")): d for d in _devices_of(before)}
    if device_id not in known:
        print(f"No paired device {device_id}. See `tst devices`.", file=sys.stderr)
        return 1
    await _send(ws, {"type": "remove_remote_device", "device_id": device_id})
    reply = await _recv_event(ws, 15.0)
    if reply.get("type") == "error":
        _print_error(reply)
        return 1
    print(f"Removed: {known[device_id].get('name')}")
    return 0


async def list_devices(data_dir: Path) -> int:
    """``tst devices``: the paired devices from ``setup_state``."""
    return await _with_daemon(data_dir, _list)


async def remove_device(data_dir: Path, device_id: str) -> int:
    """``tst devices remove <id>``: forget one device and drop its connections."""
    return await _with_daemon(data_dir, lambda ws: _remove(ws, device_id))


def run_pairing_command(args: argparse.Namespace, data_dir: Path) -> int:
    """Dispatch ``pair`` / ``devices`` from :func:`tstd.cli.main`."""
    if args.command == "pair":
        return asyncio.run(pair_device(data_dir, wait=not args.no_wait))
    if getattr(args, "devices_command", None) == "remove":
        return asyncio.run(remove_device(data_dir, args.device_id))
    return asyncio.run(list_devices(data_dir))
