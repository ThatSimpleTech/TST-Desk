"""Shared fakes for the TD-5015 tests (not collected: no ``test_`` prefix).

``FakeKeychain`` replaces the three machine-key seams with an in-memory
store (the suite's autouse isolation would make them raise). ``LOCAL``
is the dial rule the tests inject: 127.0.0.1 only, on the ``/phone``
path the other daemon's test hook counts as remote. Production keeps
the Tailscale rule.

``Fleet`` starts real in-process daemons on real 127.0.0.1 sockets. Its
``tailscale0`` stand-in (100.64.1.5) is faked only at the listener
seam, so the "Tailscale" listener is just ``/phone`` on loopback, and
every daemon dials with ``LOCAL``. ``window`` is a handshaken loopback
client and ``ask`` sends one frame and waits for its reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import ServerConnection
from websockets.asyncio.server import serve as real_serve

from tests.test_remote_attach import _fake_serve
from tstd.daemon import Daemon
from tstd.keychain import KeychainError, KeychainLockedError
from tstd.machine_pairing import DialPolicy
from tstd.protocol import PROTOCOL_VERSION

PHONE_PATH = "/phone"
LOCAL = DialPolicy(allowed=lambda host: host == "127.0.0.1", path=PHONE_PATH)


class FakeKeychain:
    """The machine keys, in memory. ``locked`` refuses every call."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}
        self.locked = False
        self.fail_store = False
        self.reads = 0

    async def store(self, mch: str, key: str) -> None:
        if self.locked:
            raise KeychainLockedError("locked")
        if self.fail_store:
            raise KeychainError("store failed")
        self.keys[mch] = key

    async def get(self, mch: str) -> str:
        self.reads += 1
        if self.locked:
            raise KeychainLockedError("locked")
        if mch not in self.keys:
            raise KeychainError("not found")
        return self.keys[mch]

    async def delete(self, mch: str) -> None:
        if self.locked:
            raise KeychainLockedError("locked")
        if mch not in self.keys:
            raise KeychainError("not found")
        del self.keys[mch]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
        monkeypatch.setattr("tstd.keychain.store_machine_key", self.store)
        monkeypatch.setattr("tstd.keychain.get_machine_key", self.get)
        monkeypatch.setattr("tstd.keychain.delete_machine_key", self.delete)
        return self


class Clock:
    """A settable monotonic clock."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def hello(token: str, features: list[str] | None = None) -> str:
    frame: dict[str, Any] = {"type": "hello", "token": token, "version": PROTOCOL_VERSION}
    if features is not None:
        frame["features"] = features
    return json.dumps(frame)


async def recv(ws: ClientConnection, within: float = 5.0) -> dict[str, Any]:
    """Next frame, skipping application pings."""
    while True:
        frame: dict[str, Any] = json.loads(await asyncio.wait_for(ws.recv(), within))
        if frame.get("type") != "ping":
            return frame


async def recv_until(
    ws: ClientConnection, want: Callable[[dict[str, Any]], bool], within: float = 10.0
) -> dict[str, Any]:
    """The first frame *want* accepts; earlier ones are skipped."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while True:
        frame = await recv(ws, max(0.05, deadline - loop.time()))
        if want(frame):
            return frame


async def eventually(check: Callable[[], bool], within: float = 5.0) -> None:
    """Wait until *check* holds; fail the test if it never does."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not check():
        if loop.time() > deadline:
            raise AssertionError("condition never held")
        await asyncio.sleep(0.01)


async def until(check: Callable[[], Awaitable[bool]], within: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not await check():
        if loop.time() > deadline:
            raise AssertionError("condition never held")
        await asyncio.sleep(0.02)


# ── real daemons on 127.0.0.1 ────────────────────────────────────────

TAILSCALE_IP = "100.64.1.5"


def tailscale_interfaces() -> dict[str, tuple[str, ...]]:
    return {"tailscale0": (TAILSCALE_IP,)}


def is_phone(ws: ServerConnection) -> bool:
    """The test hook: a connection on ``/phone`` counts as remote."""
    return ws.request is not None and ws.request.path == PHONE_PATH


def loopback_serve() -> Callable[..., Awaitable[Any]]:
    """Real sockets on 127.0.0.1; the Tailscale listener is faked.

    Patched before any daemon starts, so a restart with remote attach on
    binds its remembered loopback port and never touches 100.64.1.5.
    """
    fake = _fake_serve([])

    async def serve(handler: Any, host: str, port: int, **kwargs: Any) -> Any:
        if host == "127.0.0.1":
            return await real_serve(handler, host, port, **kwargs)
        return await fake(handler, host, port, **kwargs)

    return serve


class Fleet:
    """In-process daemons over real loopback sockets, stopped gracefully."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tstd.ws.serve", loopback_serve())
        self._running: dict[int, tuple[Daemon, asyncio.Task[None]]] = {}

    async def start(self, data_dir: Path, provider: Any = None) -> Daemon:
        daemon = Daemon(
            data_dir=data_dir,
            provider=provider,
            interfaces=tailscale_interfaces,
            dial_policy=LOCAL,
        )
        daemon.ws_server._is_remote_connection = is_phone
        task = asyncio.create_task(daemon.run())
        await eventually(lambda: daemon.ws_server.port > 0 or task.done(), within=10)
        if task.done():
            task.result()
        self._running[id(daemon)] = (daemon, task)
        return daemon

    async def stop(self, daemon: Daemon) -> None:
        entry = self._running.pop(id(daemon), None)
        if entry is None:
            return
        daemon._shutdown_event.set()
        await asyncio.wait_for(entry[1], 10)

    async def close(self) -> None:
        for daemon, _ in list(self._running.values()):
            await self.stop(daemon)


@contextlib.asynccontextmanager
async def window(
    daemon: Daemon, features: list[str] | None = None, *, token: str | None = None, path: str = "/"
) -> AsyncIterator[ClientConnection]:
    """A handshaken connection, closed on exit. Loopback with the window token by default."""
    async with connect(f"ws://127.0.0.1:{daemon.ws_server.port}{path}") as ws:
        await ws.send(hello(token or daemon.ws_server.token, features))
        ack = await recv(ws)
        assert ack["type"] == "hello_ack", ack
        yield ws


async def ask(
    ws: ClientConnection, frame: dict[str, Any], want: str | None = None
) -> dict[str, Any]:
    """Send *frame*; the first reply of type *want* (any reply when None)."""
    await ws.send(json.dumps(frame))
    if want is None:
        return await recv(ws)
    return await recv_until(ws, lambda f: f.get("type") in (want, "error"))
