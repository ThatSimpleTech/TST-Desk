"""TD-5005: the phone's address and token stay put until the user changes them.

Port: remembered beside the remote-attach switch, reused on the next start,
and a busy port falls back to a random one instead of failing to start.
Token: survives shutdown, rotates on request (dropping remote clients), and
is deleted when remote attach is turned off.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tests.test_remote_attach import _ask, _connect_and_handshake, _fake_serve
from tstd.config import RemoteConfig
from tstd.daemon import Daemon
from tstd.protocol import PROTOCOL_VERSION, RotateRemoteToken, parse_client_message
from tstd.remote_attach import (
    load_remote_attach,
    load_remote_port,
    save_remote_attach,
    save_remote_port,
)
from tstd.remote_auth import read_remote_token_file
from tstd.ws import WebSocketServer


async def _hello(port: int, token: str) -> dict[str, object]:
    async with connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
        reply: dict[str, object] = json.loads(await ws.recv())
        return reply


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _remote_server(data: Path, port: int = 0) -> WebSocketServer:
    return WebSocketServer(data, ping_interval=0, is_remote_connection=lambda _ws: True, port=port)


# ── port ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_preferred_port_is_used(tmp_path: Path) -> None:
    want = _free_port()
    server = _remote_server(tmp_path, want)
    await server.start()
    try:
        assert server.port == want
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_busy_preferred_port_falls_back_to_a_random_one(tmp_path: Path) -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        busy = holder.getsockname()[1]
        server = _remote_server(tmp_path, busy)
        await server.start()
        try:
            assert server.port not in (0, busy)
        finally:
            await server.stop()


def test_the_port_is_remembered_beside_the_switch(tmp_path: Path) -> None:
    save_remote_attach(tmp_path, True, "tailscale0")
    save_remote_port(tmp_path, 47_800)
    assert load_remote_port(tmp_path) == 47_800
    # Rewriting the switch keeps the port; the switch fields are unchanged.
    save_remote_attach(tmp_path, True, "100.64.7.7")
    assert load_remote_port(tmp_path) == 47_800
    assert load_remote_attach(tmp_path) == (True, "100.64.7.7")


@pytest.mark.parametrize("bad", [0, 80, 70_000, -1])
def test_an_unusable_port_is_not_remembered(tmp_path: Path, bad: int) -> None:
    save_remote_port(tmp_path, bad)
    assert load_remote_port(tmp_path) == 0


@pytest.mark.parametrize("bad", [80, 70_000])
def test_config_rejects_a_port_outside_the_user_range(bad: int) -> None:
    with pytest.raises(ValueError, match=r"remote\.port"):
        RemoteConfig(port=bad)


# ── token ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotate_replaces_the_token_and_the_old_one_fails(tmp_path: Path) -> None:
    server = _remote_server(tmp_path)
    await server.start()
    server._extra_host = "100.64.0.9"  # test seam: pretend the Tailscale listener is up
    try:
        old = server.remote_token
        assert await server.rotate_remote_token()
        new = server.remote_token
        assert new != old
        assert read_remote_token_file(tmp_path) == new
        assert (await _hello(server.port, old))["code"] == "auth_failed"
        assert (await _hello(server.port, new))["type"] == "hello_ack"
    finally:
        server._extra_host = None
        await server.stop()


@pytest.mark.asyncio
async def test_rotate_drops_a_connected_remote_client(tmp_path: Path) -> None:
    server = _remote_server(tmp_path)
    await server.start()
    server._extra_host = "100.64.0.9"
    try:
        async with connect(f"ws://127.0.0.1:{server.port}") as ws:
            hello = {"type": "hello", "token": server.remote_token, "version": PROTOCOL_VERSION}
            await ws.send(json.dumps(hello))
            assert json.loads(await ws.recv())["type"] == "hello_ack"
            await server.rotate_remote_token()
            await ws.wait_closed()
            assert ws.close_code is not None
    finally:
        server._extra_host = None
        await server.stop()


@pytest.mark.asyncio
async def test_rotate_without_a_remote_listener_does_nothing(tmp_path: Path) -> None:
    server = WebSocketServer(tmp_path, ping_interval=0)
    await server.start()
    try:
        assert not await server.rotate_remote_token()
        assert read_remote_token_file(tmp_path) is None
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_turning_remote_attach_off_deletes_the_token(tmp_path: Path) -> None:
    server = _remote_server(tmp_path)
    await server.start()
    try:
        assert read_remote_token_file(tmp_path) is not None
        server._extra_host = "100.64.0.9"  # so apply_bind("") sees a change
        await server.apply_bind("")
        assert read_remote_token_file(tmp_path) is None
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_damaged_token_file_is_replaced(tmp_path: Path) -> None:
    (tmp_path / "remote-token").write_text("not-a-token", encoding="utf-8")
    server = _remote_server(tmp_path)
    await server.start()
    try:
        token = server.remote_token
        assert len(token) == 64 and token != "not-a-token"
        assert read_remote_token_file(tmp_path) == token
        assert (tmp_path / "remote-token").stat().st_mode & 0o777 == 0o600
    finally:
        await server.stop()


def test_the_rotate_message_parses_and_carries_no_token() -> None:
    msg = parse_client_message('{"type": "rotate_remote_token"}')
    assert isinstance(msg, RotateRemoteToken)
    assert set(RotateRemoteToken.model_fields) == {"type"}


# ── daemon wiring ────────────────────────────────────────────────────

_EXTRA = "100.64.1.5"


def _interfaces() -> dict[str, tuple[str, ...]]:
    return {"tailscale0": (_EXTRA,)}


async def _run(tmp: Path) -> tuple[Daemon, asyncio.Task[Any]]:
    daemon = Daemon(data_dir=tmp, interfaces=_interfaces)
    task = asyncio.create_task(daemon.run())
    for _ in range(60):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


async def _halt(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_enabling_remembers_the_port_and_setup_state_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, task = await _run(tmp_path)
    try:
        ws = await _connect_and_handshake(
            f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
        )
        monkeypatch.setattr("tstd.ws.serve", _fake_serve([]))
        resp = await _ask(ws, {"type": "set_remote_attach", "enabled": True})
        assert resp["remote_port"] == daemon.ws_server.port
        assert load_remote_port(tmp_path) == daemon.ws_server.port

        before = read_remote_token_file(tmp_path)
        resp = await _ask(ws, {"type": "rotate_remote_token"})
        assert resp["type"] == "setup_state"
        assert "token" not in resp and "remote_token" not in resp
        assert read_remote_token_file(tmp_path) not in (None, before)
        await ws.close()
    finally:
        await _halt(task)


@pytest.mark.asyncio
async def test_a_restart_reuses_the_remembered_port_and_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_remote_attach(tmp_path, True, "tailscale0")
    save_remote_port(tmp_path, 47_811)
    (tmp_path / "remote-token").write_text("ab" * 32, encoding="utf-8")
    recorded: list[tuple[str, int]] = []
    monkeypatch.setattr("tstd.ws.serve", _fake_serve(recorded))
    daemon, task = await _run(tmp_path)
    try:
        assert daemon.ws_server.port == 47_811
        assert ("127.0.0.1", 47_811) in recorded and (_EXTRA, 47_811) in recorded
        assert daemon.ws_server.remote_token == "ab" * 32
    finally:
        await _halt(task)
