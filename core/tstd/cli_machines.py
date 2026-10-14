"""`tst machines` (TD-5015): list, add and remove paired computers.

Talks to the running daemon over loopback with the port-file token, like
``tst devices``; the machine verbs are loopback-only. ``add`` takes a
``tstdesk://pair`` link, or ``host:port`` and the code the other computer
shows. The code and the key the pairing makes are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .cli import CliError, _print_error, _recv_event, _send
from .cli_pairing import _address, _with_daemon

# Pairing dials the other computer (10 s) and writes the keychain; the
# daemon answers either way, this is only the CLI's own patience.
_ADD_WAIT_SECS = 45.0
_REPLY_WAIT_SECS = 15.0


def add_machine_commands(sub: Any, add_data_dir: Callable[[argparse.ArgumentParser], None]) -> None:
    """Register ``machines [add|remove]`` on the ``tst`` parser."""
    machines_p = sub.add_parser("machines", help="list paired computers, or add or remove one")
    add_data_dir(machines_p)
    machines_sub = machines_p.add_subparsers(dest="machines_command")
    add_p = machines_sub.add_parser("add", help="pair another computer with this one")
    add_p.add_argument("target", help="a tstdesk://pair link, or the other computer's host:port")
    add_p.add_argument("code", nargs="?", help="the pairing code it shows (with host:port)")
    # SUPPRESS: a default here would overwrite `tst machines --data-dir X add`.
    add_p.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS)
    remove_p = machines_sub.add_parser("remove", help="forget a paired computer")
    remove_p.add_argument("machine_id", help="computer id from `tst machines`")
    remove_p.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS)


def split_address(text: str) -> tuple[str, int]:
    """``host:port`` (an IPv6 host in brackets) as a pair. Raises CliError."""
    raw = text.strip()
    if raw.startswith("["):
        host, sep, rest = raw[1:].partition("]")
        if not sep or not rest.startswith(":"):
            raise CliError("give the address as host:port, with an IPv6 host in [brackets]")
        port_text = rest[1:]
    else:
        host, sep, port_text = raw.rpartition(":")
        if not sep or ":" in host:
            raise CliError("give the address as host:port, with an IPv6 host in [brackets]")
    try:
        port = int(port_text)
    except ValueError:
        raise CliError("the port must be a number") from None
    if not host or not 1 <= port <= 65535:
        raise CliError("give the address as host:port")
    return host, port


def add_frame(target: str, code: str | None) -> dict[str, Any]:
    """The ``add_machine`` frame for the CLI's two forms. Raises CliError."""
    if target.strip().startswith("tstdesk:"):
        if code is not None:
            raise CliError("a pairing link already carries its code; give only the link")
        return {"type": "add_machine", "link": target.strip()}
    if code is None:
        raise CliError("give the pairing code after host:port, or paste the whole link")
    host, port = split_address(target)
    return {"type": "add_machine", "host": host, "port": port, "code": code}


def _target_address(frame: dict[str, Any]) -> tuple[object, object]:
    """The host and port the frame pairs with, for naming the result."""
    link = frame.get("link")
    if not isinstance(link, str):
        return frame.get("host"), frame.get("port")
    query = parse_qs(urlsplit(link).query)
    port = (query.get("p") or [""])[0]
    return (query.get("h") or [""])[0], int(port) if port.isdigit() else None


def _machines_of(event: dict[str, Any]) -> list[dict[str, Any]]:
    raw = event.get("machines")
    return [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []


def _label(machine: dict[str, Any]) -> str:
    name = str(machine.get("name") or "a computer")
    return f"{name} (this computer)" if machine.get("same_computer") else name


async def _await_list(ws: Any, wait: float) -> list[dict[str, Any]] | None:
    """The next ``machine_list``; an ``error`` is printed and gives None."""
    while True:
        event = await _recv_event(ws, wait)
        if event.get("type") == "machine_list":
            return _machines_of(event)
        if event.get("type") == "error":
            _print_error(event)
            return None


async def _current(ws: Any) -> list[dict[str, Any]] | None:
    await _send(ws, {"type": "list_machines"})
    return await _await_list(ws, _REPLY_WAIT_SECS)


async def _list(ws: Any) -> int:
    machines = await _current(ws)
    if machines is None:
        return 1
    if not machines:
        print("No paired computers. Run `tst machines add <link>` to add one.")
        return 0
    for m in machines:
        seen = m.get("last_seen") or "never"
        where = _address(m.get("host"), m.get("port"))
        print(f"{m.get('id')}  {_label(m)}  {where}  {m.get('status')}  last seen {seen}")
    return 0


async def _add(ws: Any, frame: dict[str, Any]) -> int:
    await _send(ws, frame)
    print("Pairing... (this can take a few seconds)")
    sys.stdout.flush()
    machines = await _await_list(ws, _ADD_WAIT_SECS)
    if machines is None:
        return 1
    host, port = _target_address(frame)
    match = next((m for m in machines if (m.get("host"), m.get("port")) == (host, port)), None)
    print(f"Paired: {_label(match)}" if match is not None else "Paired.")
    return 0


async def _remove(ws: Any, machine_id: str) -> int:
    before = await _current(ws)
    if before is None:
        return 1
    known = {str(m.get("id")): m for m in before}
    if machine_id not in known:
        print(f"No paired computer {machine_id}. See `tst machines`.", file=sys.stderr)
        return 1
    await _send(ws, {"type": "remove_machine", "machine": machine_id})
    if await _await_list(ws, _REPLY_WAIT_SECS) is None:
        return 1
    print(f"Removed: {_label(known[machine_id])}")
    return 0


async def list_machines(data_dir: Path) -> int:
    """``tst machines``."""
    return await _with_daemon(data_dir, _list)


async def add_machine(data_dir: Path, target: str, code: str | None) -> int:
    """``tst machines add``. Raises CliError for a malformed target."""
    frame = add_frame(target, code)
    return await _with_daemon(data_dir, lambda ws: _add(ws, frame))


async def remove_machine(data_dir: Path, machine_id: str) -> int:
    """``tst machines remove``."""
    return await _with_daemon(data_dir, lambda ws: _remove(ws, machine_id))


def run_machines_command(args: argparse.Namespace, data_dir: Path) -> int:
    """Dispatch ``machines`` from :func:`tstd.cli.main`."""
    command = getattr(args, "machines_command", None)
    if command == "add":
        return asyncio.run(add_machine(data_dir, args.target, args.code))
    if command == "remove":
        return asyncio.run(remove_machine(data_dir, args.machine_id))
    return asyncio.run(list_machines(data_dir))
