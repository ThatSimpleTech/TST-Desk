"""Remote-attach persistence (TD-3603) and Settings toggle."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tstd.daemon import Daemon
from tstd.protocol import PROTOCOL_VERSION
from tstd.remote_attach import (
    DEFAULT_REMOTE_BIND,
    bind_spec_when_enabled,
    load_remote_attach,
    remote_attach_path,
    save_remote_attach,
)


class TestRemoteAttachPersist:
    def test_absent_is_off(self, tmp_path: Path) -> None:
        enabled, last_bind = load_remote_attach(tmp_path)
        assert enabled is False
        assert last_bind == DEFAULT_REMOTE_BIND

    def test_round_trip(self, tmp_path: Path) -> None:
        save_remote_attach(tmp_path, True, "100.64.1.5")
        assert load_remote_attach(tmp_path) == (True, "100.64.1.5")
        save_remote_attach(tmp_path, False, "tailscale0")
        assert load_remote_attach(tmp_path) == (False, "tailscale0")

    def test_lands_in_user_data_not_workspace_config(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        workspace = tmp_path / "ws"
        (workspace / ".tst").mkdir(parents=True)
        workspace_config = workspace / ".tst" / "config.yaml"
        workspace_config.write_text("policy:\n  rules: []\n", encoding="utf-8")
        before = workspace_config.read_text(encoding="utf-8")

        save_remote_attach(data, True, "tailscale0")

        assert remote_attach_path(data).exists()
        assert not (workspace / "remote-attach.yaml").exists()
        assert workspace_config.read_text(encoding="utf-8") == before

    def test_unreadable_or_junk_is_off(self, tmp_path: Path) -> None:
        path = remote_attach_path(tmp_path)
        path.write_text(":::: not yaml", encoding="utf-8")
        assert load_remote_attach(tmp_path) == (False, DEFAULT_REMOTE_BIND)
        path.write_text("- just a list\n", encoding="utf-8")
        assert load_remote_attach(tmp_path) == (False, DEFAULT_REMOTE_BIND)
        path.write_text("enabled: true\nlast_bind: utun3\n", encoding="utf-8")
        assert load_remote_attach(tmp_path) == (True, "utun3")
        path.write_text("enabled: false\n", encoding="utf-8")
        assert load_remote_attach(tmp_path) == (False, DEFAULT_REMOTE_BIND)

    def test_bind_spec_prefers_last_known(self) -> None:
        assert bind_spec_when_enabled("100.64.9.2", "") == "100.64.9.2"
        assert bind_spec_when_enabled("", "utun3") == "utun3"
        assert bind_spec_when_enabled("", "") == DEFAULT_REMOTE_BIND


async def _connect_and_handshake(uri: str, token: str) -> Any:
    ws = await connect(uri)
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _start_daemon(tmp: Path, **kwargs: Any) -> tuple[Daemon, asyncio.Task[Any]]:
    daemon = Daemon(data_dir=tmp, **kwargs)
    task = asyncio.create_task(daemon.run())
    for _ in range(50):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


async def _stop_daemon(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _ask(ws: Any, msg: dict[str, Any]) -> dict[str, Any]:
    await ws.send(json.dumps(msg))
    return dict(json.loads(await ws.recv()))


def _fake_serve(recorded: list[tuple[str, int]]) -> Any:
    class _FakeSock:
        def __init__(self, host: str, port: int) -> None:
            self._host = host
            self._port = port

        def getsockname(self) -> tuple[str, int]:
            return (self._host, self._port)

    class _FakeServer:
        def __init__(self, host: str, port: int) -> None:
            self.sockets = [_FakeSock(host, port)]

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def fake_serve(_handler: object, host: str, port: int, **_kwargs: Any) -> _FakeServer:
        bound = 54321 if port == 0 else port
        recorded.append((host, bound))
        return _FakeServer(host, bound)

    return fake_serve


class TestRemoteAttachSettingsWire:
    async def test_setup_state_defaults_off(self, tmp_path: Path) -> None:
        daemon, task = await _start_daemon(tmp_path)
        try:
            assert daemon.remote_attach_enabled is False
            assert daemon.ws_server.extra_host is None
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            resp = await _ask(ws, {"type": "get_setup_state"})
            assert resp["type"] == "setup_state"
            assert resp["remote_attach_enabled"] is False
            assert resp["remote_bind"] is None
            assert "remote_token" not in resp
            assert "token" not in resp
            await ws.close()
        finally:
            await _stop_daemon(task)

    async def test_toggle_on_reports_extra_host_off_leaves_loopback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extra = "100.64.1.5"

        def interfaces() -> dict[str, tuple[str, ...]]:
            return {"tailscale0": (extra,)}

        daemon, task = await _start_daemon(tmp_path, interfaces=interfaces)
        try:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            recorded: list[tuple[str, int]] = []
            monkeypatch.setattr("tstd.ws.serve", _fake_serve(recorded))
            resp = await _ask(ws, {"type": "set_remote_attach", "enabled": True})
            assert resp["type"] == "setup_state"
            assert resp["remote_attach_enabled"] is True
            assert resp["remote_bind"] == extra
            assert "remote_token" not in resp
            assert "token" not in resp
            assert daemon.ws_server.extra_host == extra
            assert extra in {host for host, _ in recorded}
            assert "0.0.0.0" not in {host for host, _ in recorded}
            assert load_remote_attach(tmp_path) == (True, DEFAULT_REMOTE_BIND)

            resp = await _ask(ws, {"type": "set_remote_attach", "enabled": False})
            assert resp["remote_attach_enabled"] is False
            assert resp["remote_bind"] is None
            assert daemon.ws_server.extra_host is None
            assert all(host in {"127.0.0.1", "::1"} for host in daemon.ws_server.bound_hosts)
            assert load_remote_attach(tmp_path)[0] is False
            await ws.close()
        finally:
            await _stop_daemon(task)

    async def test_off_survives_restart(self, tmp_path: Path) -> None:
        save_remote_attach(tmp_path, True, "tailscale0")
        daemon, task = await _start_daemon(tmp_path)
        try:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            await _ask(ws, {"type": "set_remote_attach", "enabled": False})
            await ws.close()
        finally:
            await _stop_daemon(task)

        daemon2, task2 = await _start_daemon(tmp_path)
        try:
            assert daemon2.remote_attach_enabled is False
            assert daemon2.ws_server.extra_host is None
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon2.ws_server.port}", daemon2.ws_server.token
            )
            resp = await _ask(ws, {"type": "get_setup_state"})
            assert resp["remote_attach_enabled"] is False
            assert resp["remote_bind"] is None
            await ws.close()
        finally:
            await _stop_daemon(task2)
