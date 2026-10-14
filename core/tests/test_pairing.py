"""TD-5014: pairing a phone over the real socket, and the daemon's verbs.

A remote peer may send ``pair`` instead of ``hello`` as its first frame,
gets its own key in ``pair_ok``, and reconnects with that key. The
computer itself (loopback) is the only one that can start pairing or
remove a device. Remote peers are simulated by connecting to the
loopback listener on ``/phone`` with ``is_remote_connection`` injected.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import ServerConnection

from tests.test_remote_attach import _fake_serve
from tests.test_remote_devices import FakeClock
from tstd.daemon import Daemon
from tstd.protocol import (
    PROTOCOL_VERSION,
    CancelPairing,
    Pair,
    PairingCompleted,
    RemoteDeviceSummary,
    RemoveRemoteDevice,
    StartPairing,
    parse_client_message,
    parse_daemon_event,
)
from tstd.remote_attach import load_remote_attach
from tstd.remote_devices import (
    DEVICES_UNAVAILABLE_MESSAGE,
    MAX_DEVICES,
    PAIR_FAILED_MESSAGE,
    PAIR_LOCKED_MESSAGE,
    PAIRING_FILE_UNAVAILABLE_MESSAGE,
    REMOTE_DEVICES_FILE,
    PairingCodes,
    RemoteDeviceRegistry,
)
from tstd.ws import WebSocketServer

PHONE = "/phone"
_EXTRA = "100.64.1.5"


def _is_phone(ws: ServerConnection) -> bool:
    return ws.request is not None and ws.request.path == PHONE


def _hello(token: str) -> str:
    return json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION})


async def _recv(ws: ClientConnection, within: float = 5.0) -> dict[str, Any]:
    """Next frame, skipping application pings."""
    while True:
        frame: dict[str, Any] = json.loads(await asyncio.wait_for(ws.recv(), within))
        if frame.get("type") != "ping":
            return frame


async def pair(
    port: int,
    code: str,
    name: str = "Pixel 11 Pro Fold",
    path: str = PHONE,
    version: int = PROTOCOL_VERSION,
    replaces: str | None = None,
) -> tuple[dict[str, Any], int | None]:
    """Send one pair frame. Returns the reply and the close code."""
    async with connect(f"ws://127.0.0.1:{port}{path}") as ws:
        frame: dict[str, Any] = {
            "type": "pair",
            "code": code,
            "device_name": name,
            "version": version,
        }
        if replaces is not None:
            frame["replaces"] = replaces
        await ws.send(json.dumps(frame))
        reply = await _recv(ws)
        await asyncio.wait_for(ws.wait_closed(), 5)
        return reply, ws.close_code


async def hello_reply(port: int, token: str, path: str = PHONE) -> dict[str, Any]:
    async with connect(f"ws://127.0.0.1:{port}{path}") as ws:
        await ws.send(_hello(token))
        return await _recv(ws)


@contextlib.asynccontextmanager
async def attached(port: int, token: str, path: str = PHONE) -> AsyncIterator[ClientConnection]:
    """A handshaken connection, closed on exit."""
    async with connect(f"ws://127.0.0.1:{port}{path}") as ws:
        await ws.send(_hello(token))
        ack = await _recv(ws)
        assert ack["type"] == "hello_ack", ack
        yield ws


# ── the socket ───────────────────────────────────────────────────────


class _Server:
    def __init__(self, tmp: Path) -> None:
        self.clock = FakeClock()
        self.devices = RemoteDeviceRegistry(tmp, clock=self.clock)
        self.devices.load()
        self.pairing = PairingCodes(clock=self.clock)
        self.paired: list[RemoteDeviceSummary] = []
        # (device id, connected) at each presence callback.
        self.presence: list[tuple[str, bool]] = []
        self.ws = WebSocketServer(
            tmp,
            ping_interval=0,
            is_remote_connection=_is_phone,
            devices=self.devices,
            pairing=self.pairing,
            on_paired=self._on_paired,
            on_device_presence=self._on_presence,
        )

    async def _on_paired(self, device: RemoteDeviceSummary) -> None:
        self.paired.append(device)

    async def _on_presence(self, device_id: str) -> None:
        self.presence.append((device_id, device_id in self.ws.connected_device_ids()))

    async def settled(self, count: int) -> list[tuple[str, bool]]:
        """The presence calls, once there are *count* of them."""
        for _ in range(200):
            if len(self.presence) >= count:
                break
            await asyncio.sleep(0.01)
        return self.presence

    @property
    def port(self) -> int:
        return self.ws.port


@pytest.fixture
async def server(tmp_path: Path) -> AsyncIterator[_Server]:
    srv = _Server(tmp_path)
    await srv.ws.start()
    try:
        yield srv
    finally:
        await srv.ws.stop()


async def test_a_phone_pairs_then_connects_with_its_own_key(server: _Server) -> None:
    code, _ = server.pairing.start()
    reply, close_code = await pair(server.port, code[:4] + "-" + code[4:].lower())
    assert close_code == 1000
    assert reply["type"] == "pair_ok"
    assert set(reply) == {
        "type",
        "version",
        "device_id",
        "device_name",
        "token",
        "desktop_name",
        "machine_id",
    }
    assert reply["version"] == PROTOCOL_VERSION
    assert reply["device_name"] == "Pixel 11 Pro Fold"
    assert reply["token"] not in (server.ws.token, server.ws.remote_token)
    # on_paired runs once the close handshake is done, so give it a moment.
    for _ in range(100):
        if server.paired:
            break
        await asyncio.sleep(0.01)
    assert [d.id for d in server.paired] == [reply["device_id"]]

    async with attached(server.port, reply["token"]):
        assert server.ws.connected_device_ids() == {reply["device_id"]}
        assert server.devices.summaries()[0].last_seen is not None
    # The code was single use.
    again, _ = await pair(server.port, code)
    assert again["code"] == "pair_failed"


async def test_a_wrong_code_fails_and_closes_1008(server: _Server) -> None:
    code, _ = server.pairing.start()
    wrong = "0000000" + ("1" if code[-1] != "1" else "2")
    reply, close_code = await pair(server.port, wrong)
    assert reply == {"type": "error", "code": "pair_failed", "message": PAIR_FAILED_MESSAGE}
    assert close_code == 1008
    assert len(server.devices) == 0
    ok, _ = await pair(server.port, code)
    assert ok["type"] == "pair_ok"


async def test_no_active_code_and_an_expired_code_fail(server: _Server) -> None:
    reply, _ = await pair(server.port, "ABCD2345")
    assert reply["code"] == "pair_failed"
    code, _ = server.pairing.start()
    server.clock.now += 601
    reply, _ = await pair(server.port, code)
    assert reply["code"] == "pair_failed"
    assert server.paired == []


async def test_five_wrong_codes_lock_pairing(server: _Server) -> None:
    code, _ = server.pairing.start()
    wrong = "0000000" + ("1" if code[-1] != "1" else "2")
    codes = [(await pair(server.port, wrong))[0]["code"] for _ in range(5)]
    assert codes == ["pair_failed"] * 4 + ["pair_locked"]
    reply, close_code = await pair(server.port, code)
    assert reply == {"type": "error", "code": "pair_locked", "message": PAIR_LOCKED_MESSAGE}
    assert close_code == 1008
    assert len(server.devices) == 0


async def test_a_loopback_pair_is_refused_and_spends_nothing(server: _Server) -> None:
    code, _ = server.pairing.start()
    reply, close_code = await pair(server.port, code, path="/")
    assert reply["code"] == "bad_request"
    assert reply["message"] == "This computer is already connected; pairing is for other devices."
    assert close_code == 1008
    ok, _ = await pair(server.port, code)
    assert ok["type"] == "pair_ok"


async def test_a_malformed_pair_frame_is_bad_request_and_never_echoes_the_code(
    server: _Server,
) -> None:
    code, _ = server.pairing.start()
    async with connect(f"ws://127.0.0.1:{server.port}{PHONE}") as ws:
        await ws.send(json.dumps({"type": "pair", "code": code}))  # no version
        reply = await _recv(ws)
    assert reply["code"] == "bad_request"
    assert code not in reply["message"]
    reply, _ = await pair(server.port, code, version=PROTOCOL_VERSION + 1)
    assert reply["code"] == "version_unsupported"
    # Neither counted as a try.
    ok, _ = await pair(server.port, code)
    assert ok["type"] == "pair_ok"


async def test_codes_and_keys_are_never_logged(
    server: _Server, caplog: pytest.LogCaptureFixture
) -> None:
    code, _ = server.pairing.start()
    with caplog.at_level(logging.DEBUG):
        await pair(server.port, "0000000Z" if code != "0000000Z" else "0000000Y")
        reply, _ = await pair(server.port, code)
        async with attached(server.port, reply["token"]):
            pass
    # Only the daemon's own logs; websockets' DEBUG frame dumps are never on.
    ours = [r for r in caplog.records if r.name.startswith("tstd")]
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in ours)
    assert "pairing redeemed" in logged
    assert code not in logged
    assert reply["token"] not in logged


async def test_the_legacy_remote_token_still_works(server: _Server) -> None:
    async with attached(server.port, server.ws.remote_token):
        assert server.ws.connected_device_ids() == set()


async def test_a_device_key_is_not_a_loopback_token(server: _Server) -> None:
    code, _ = server.pairing.start()
    reply, _ = await pair(server.port, code)
    refused = await hello_reply(server.port, reply["token"], path="/")
    assert refused["code"] == "auth_failed"
    assert (await hello_reply(server.port, "ab" * 32))["code"] == "auth_failed"


async def _pair_two(server: _Server) -> tuple[dict[str, Any], dict[str, Any]]:
    code, _ = server.pairing.start()
    first, _ = await pair(server.port, code, name="Phone A")
    code, _ = server.pairing.start()
    second, _ = await pair(server.port, code, name="Phone B")
    return first, second


async def test_closing_a_device_drops_only_its_connections(server: _Server) -> None:
    a, b = await _pair_two(server)
    async with (
        attached(server.port, a["token"]) as ws_a,
        attached(server.port, b["token"]) as ws_b,
        attached(server.port, server.ws.remote_token) as ws_legacy,
    ):
        assert await server.ws.close_device(a["device_id"]) == 1
        await asyncio.wait_for(ws_a.wait_closed(), 5)
        assert ws_a.close_code == 1008
        assert server.ws.connected_device_ids() == {b["device_id"]}
        await asyncio.wait_for(ws_b.ping(), 5)
        await asyncio.wait_for(ws_legacy.ping(), 5)


async def test_rotating_the_legacy_token_keeps_paired_devices(server: _Server) -> None:
    (a, _) = await _pair_two(server)
    server.ws._extra_host = "100.64.0.9"  # test seam: pretend the listener is up
    try:
        async with (
            attached(server.port, a["token"]) as device,
            attached(server.port, server.ws.remote_token) as legacy,
        ):
            assert await server.ws.rotate_remote_token()
            await asyncio.wait_for(legacy.wait_closed(), 5)
            await asyncio.wait_for(device.ping(), 5)
        assert (await hello_reply(server.port, a["token"]))["type"] == "hello_ack"
    finally:
        server.ws._extra_host = None


async def test_a_re_pair_retires_the_device_it_replaces(server: _Server) -> None:
    code, _ = server.pairing.start()
    old, _ = await pair(server.port, code, name="Old key")
    async with attached(server.port, old["token"]) as old_ws:
        code, _ = server.pairing.start()
        new, close_code = await pair(server.port, code, replaces=old["token"])
        assert new["type"] == "pair_ok" and close_code == 1000
        await asyncio.wait_for(old_ws.wait_closed(), 5)
        assert old_ws.close_code == 1008
    assert [s.id for s in server.devices.summaries()] == [new["device_id"]]
    assert (await hello_reply(server.port, old["token"]))["code"] == "auth_failed"
    assert (await hello_reply(server.port, new["token"]))["type"] == "hello_ack"


@pytest.mark.parametrize("replaces", ["legacy", "ab" * 32, "not a key"])
async def test_replaces_that_is_no_device_key_retires_nothing(
    server: _Server, replaces: str
) -> None:
    code, _ = server.pairing.start()
    kept, _ = await pair(server.port, code, name="Kept")
    stand_in = server.ws.remote_token if replaces == "legacy" else replaces
    code, _ = server.pairing.start()
    new, _ = await pair(server.port, code, replaces=stand_in)
    assert new["type"] == "pair_ok"
    assert {s.id for s in server.devices.summaries()} == {kept["device_id"], new["device_id"]}
    assert (await hello_reply(server.port, server.ws.remote_token))["type"] == "hello_ack"


async def test_replaces_is_never_logged(server: _Server, caplog: pytest.LogCaptureFixture) -> None:
    code, _ = server.pairing.start()
    old, _ = await pair(server.port, code)
    code, _ = server.pairing.start()
    with caplog.at_level(logging.DEBUG):
        await pair(server.port, code, replaces=old["token"])
    ours = [r for r in caplog.records if r.name.startswith("tstd")]
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in ours)
    assert "pairing redeemed" in logged
    assert old["token"] not in logged


async def test_presence_is_reported_when_a_device_connects_and_drops(server: _Server) -> None:
    code, _ = server.pairing.start()
    reply, _ = await pair(server.port, code)
    async with attached(server.port, server.ws.remote_token):
        pass
    assert server.presence == []  # neither the pair socket nor the legacy token
    async with attached(server.port, reply["token"]):
        assert await server.settled(1) == [(reply["device_id"], True)]
    assert await server.settled(2) == [(reply["device_id"], True), (reply["device_id"], False)]


async def test_a_removed_device_dropping_is_still_reported(server: _Server) -> None:
    code, _ = server.pairing.start()
    reply, _ = await pair(server.port, code)
    async with attached(server.port, reply["token"]) as ws:
        await server.settled(1)
        assert server.devices.remove(reply["device_id"])
        await server.ws.close_device(reply["device_id"])
        await asyncio.wait_for(ws.wait_closed(), 5)
    assert (await server.settled(2))[1] == (reply["device_id"], False)


async def test_stopping_reports_no_presence(tmp_path: Path) -> None:
    srv = _Server(tmp_path)
    await srv.ws.start()
    code, _ = srv.pairing.start()
    reply, _ = await pair(srv.port, code)
    async with attached(srv.port, reply["token"]) as ws:
        await srv.settled(1)
        await srv.ws.stop()
        await asyncio.wait_for(ws.wait_closed(), 5)
    assert srv.presence == [(reply["device_id"], True)]


def _make_unreadable(tmp: Path) -> Path:
    """Move the devices file away and leave a directory in its place."""
    path = tmp / REMOTE_DEVICES_FILE
    saved = tmp / "saved-devices.yaml"
    os.replace(path, saved)
    path.mkdir()
    return saved


def _make_readable(tmp: Path, saved: Path) -> None:
    (tmp / REMOTE_DEVICES_FILE).rmdir()
    os.replace(saved, tmp / REMOTE_DEVICES_FILE)


def _left_alone(tmp: Path) -> bool:
    """Kept as it was: nothing set aside, nothing written over it."""
    path = tmp / REMOTE_DEVICES_FILE
    return path.is_dir() and not list(tmp.glob(f"{REMOTE_DEVICES_FILE}.corrupt-*"))


async def test_an_unreadable_devices_file_means_retry_not_refused(tmp_path: Path) -> None:
    setup = RemoteDeviceRegistry(tmp_path)
    setup.load()
    summary, key = setup.add("Phone")
    saved = _make_unreadable(tmp_path)
    srv = _Server(tmp_path)
    assert not srv.devices.available
    await srv.ws.start()
    try:
        for token in (key, "ab" * 32):
            reply = await hello_reply(srv.port, token)
            assert reply == {
                "type": "error",
                "code": "devices_unavailable",
                "message": DEVICES_UNAVAILABLE_MESSAGE,
            }
        assert (await hello_reply(srv.port, srv.ws.remote_token))["type"] == "hello_ack"
        assert _left_alone(tmp_path)
        # The next hello reads it again, so no restart is needed.
        _make_readable(tmp_path, saved)
        assert (await hello_reply(srv.port, key))["type"] == "hello_ack"
        assert srv.devices.available and summary.id in srv.devices
        assert (await hello_reply(srv.port, "ab" * 32))["code"] == "auth_failed"
    finally:
        await srv.ws.stop()


# ── protocol shapes ──────────────────────────────────────────────────


def test_the_new_messages_parse() -> None:
    msg = parse_client_message(
        '{"type": "pair", "code": "ABCD2345", "device_name": "Pixel", "version": 1}'
    )
    assert isinstance(msg, Pair) and msg.code == "ABCD2345" and msg.replaces is None
    key = "ab" * 32
    again = parse_client_message(
        json.dumps({"type": "pair", "code": "ABCD2345", "version": 1, "replaces": key})
    )
    assert isinstance(again, Pair) and again.replaces == key
    nulled = parse_client_message(
        '{"type": "pair", "code": "ABCD2345", "version": 1, "replaces": null}'
    )
    assert isinstance(nulled, Pair) and nulled.replaces is None
    assert isinstance(parse_client_message('{"type": "start_pairing"}'), StartPairing)
    assert isinstance(parse_client_message('{"type": "cancel_pairing"}'), CancelPairing)
    removed = parse_client_message('{"type": "remove_remote_device", "device_id": "dev_x"}')
    assert isinstance(removed, RemoveRemoteDevice) and removed.device_id == "dev_x"


def test_pairing_completed_carries_a_summary_and_no_key() -> None:
    device = RemoteDeviceSummary(id="dev_0123456789ab", name="Pixel", paired_at="t")
    event = parse_daemon_event(PairingCompleted(device=device).model_dump_json())
    assert isinstance(event, PairingCompleted)
    assert set(event.device.model_dump()) == {
        "id",
        "name",
        "paired_at",
        "last_seen",
        "connected",
        "kind",
    }


# ── the daemon's verbs ───────────────────────────────────────────────


def _interfaces() -> dict[str, tuple[str, ...]]:
    return {"tailscale0": (_EXTRA,)}


def _no_tailscale() -> dict[str, tuple[str, ...]]:
    return {}


@contextlib.asynccontextmanager
async def running_daemon(tmp: Path, interfaces: Any = _interfaces) -> AsyncIterator[Daemon]:
    """A daemon whose ``/phone`` connections count as remote.

    The Tailscale listener is faked once loopback is up, as in
    test_remote_persist, and unfaked on exit so a second daemon serves.
    """
    daemon = Daemon(data_dir=tmp, interfaces=interfaces)
    daemon.ws_server._is_remote_connection = _is_phone
    task = asyncio.create_task(daemon.run())
    try:
        with pytest.MonkeyPatch.context() as mp:
            for _ in range(100):
                if daemon.ws_server.port:
                    break
                await asyncio.sleep(0.05)
            assert daemon.ws_server.port > 0
            mp.setattr("tstd.ws.serve", _fake_serve([]))
            yield daemon
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def ask(ws: ClientConnection, msg: dict[str, Any]) -> dict[str, Any]:
    await ws.send(json.dumps(msg))
    return await _recv(ws)


async def presence(window: ClientConnection) -> list[dict[str, Any]]:
    """The device list in the next frame: an unasked-for presence ``setup_state``."""
    frame = await _recv(window)
    assert frame["type"] == "setup_state", frame
    devices: list[dict[str, Any]] = frame["remote_devices"]
    return devices


async def test_start_pairing_turns_remote_attach_on_and_returns_a_code(tmp_path: Path) -> None:
    async with (
        running_daemon(tmp_path) as daemon,
        attached(daemon.ws_server.port, daemon.ws_server.token, "/") as window,
    ):
        assert daemon.remote_attach_enabled is False
        event = await ask(window, {"type": "start_pairing"})
        assert event["type"] == "pairing_code", event
        assert event["host"] == _EXTRA and event["port"] == daemon.ws_server.port
        assert event["display_code"] == f"{event['code'][:4]}-{event['code'][4:]}"
        assert event["link"].startswith(f"tstdesk://pair?h={_EXTRA}&p={event['port']}&c=")
        assert event["qr_svg"].startswith("<svg")
        assert daemon.remote_attach_enabled is True
        assert load_remote_attach(tmp_path)[0] is True
        state = await ask(window, {"type": "get_setup_state"})
        assert state["remote_attach_enabled"] is True and state["remote_bind"] == _EXTRA


async def test_no_tailscale_means_pairing_unavailable(tmp_path: Path) -> None:
    async with (
        running_daemon(tmp_path, _no_tailscale) as daemon,
        attached(daemon.ws_server.port, daemon.ws_server.token, "/") as window,
    ):
        reply = await ask(window, {"type": "start_pairing"})
        assert reply["type"] == "error"
        assert reply["code"] == "pairing_unavailable"
        assert reply["message"] == (
            "Tailscale isn't running on this computer, so a phone can't reach it."
        )
        assert "session_id" not in reply
        assert not daemon.pairing.active
        # Pairing turned remote attach on only to show a code: it goes back off.
        assert daemon.remote_attach_enabled is False
        assert load_remote_attach(tmp_path)[0] is False


async def test_no_tailscale_leaves_remote_attach_on_when_it_already_was(tmp_path: Path) -> None:
    async with (
        running_daemon(tmp_path, _no_tailscale) as daemon,
        attached(daemon.ws_server.port, daemon.ws_server.token, "/") as window,
    ):
        turned = await ask(window, {"type": "set_remote_attach", "enabled": True})
        assert turned["remote_attach_enabled"] is True
        reply = await ask(window, {"type": "start_pairing"})
        assert reply["code"] == "pairing_unavailable"
        assert daemon.remote_attach_enabled is True
        assert load_remote_attach(tmp_path)[0] is True


async def test_an_unreadable_devices_file_makes_pairing_unavailable_until_it_reads(
    tmp_path: Path,
) -> None:
    setup = RemoteDeviceRegistry(tmp_path)
    setup.load()
    setup.add("Phone")
    saved = _make_unreadable(tmp_path)
    async with (
        running_daemon(tmp_path) as daemon,
        attached(daemon.ws_server.port, daemon.ws_server.token, "/") as window,
    ):
        reply = await ask(window, {"type": "start_pairing"})
        assert reply == {
            "type": "error",
            "code": "pairing_unavailable",
            "message": PAIRING_FILE_UNAVAILABLE_MESSAGE,
        }
        assert daemon.remote_attach_enabled is False and not daemon.pairing.active
        _make_readable(tmp_path, saved)
        assert (await ask(window, {"type": "start_pairing"}))["type"] == "pairing_code"
        assert len(daemon.remote_devices) == 1


async def test_pairing_completed_reaches_the_computer_and_not_other_phones(tmp_path: Path) -> None:
    async with running_daemon(tmp_path) as daemon:
        port = daemon.ws_server.port
        async with (
            attached(port, daemon.ws_server.token, "/") as window,
            attached(port, daemon.ws_server.remote_token) as other_phone,
        ):
            code = (await ask(window, {"type": "start_pairing"}))["code"]
            reply, _ = await pair(port, code, name="  Pixel\n 11  ")
            # Control characters go and the ends are trimmed.
            assert reply["type"] == "pair_ok" and reply["device_name"] == "Pixel 11"
            done = await _recv(window)
            assert done["type"] == "pairing_completed"
            assert done["device"]["id"] == reply["device_id"]
            assert "token" not in json.dumps(done)
            with pytest.raises(TimeoutError):
                await _recv(other_phone, within=0.3)

            state = await ask(window, {"type": "get_setup_state"})
            assert [d["id"] for d in state["remote_devices"]] == [reply["device_id"]]
            assert state["remote_devices"][0]["connected"] is False
            async with attached(port, reply["token"]):
                # Presence: the computer hears of the connection unasked.
                (row,) = await presence(window)
                assert row["connected"] is True and row["last_seen"] is not None
                state = await ask(window, {"type": "get_setup_state"})
                assert state["remote_devices"] == [row]
                assert set(row) == {"id", "name", "paired_at", "last_seen", "connected", "kind"}
                assert reply["token"] not in json.dumps(state)
            (row,) = await presence(window)
            assert row["connected"] is False
            with pytest.raises(TimeoutError):
                await _recv(other_phone, within=0.3)


async def test_a_remote_peer_cannot_pair_cancel_or_remove(tmp_path: Path) -> None:
    async with running_daemon(tmp_path) as daemon:
        port = daemon.ws_server.port
        summary, _ = daemon.remote_devices.add("Existing phone")
        async with (
            attached(port, daemon.ws_server.token, "/") as window,
            attached(port, daemon.ws_server.remote_token) as phone,
        ):
            await ask(window, {"type": "start_pairing"})
            for msg in (
                {"type": "start_pairing"},
                {"type": "cancel_pairing"},
                {"type": "remove_remote_device", "device_id": summary.id},
            ):
                reply = await ask(phone, msg)
                assert reply["type"] == "error", msg
                assert reply["message"] == "Only the computer itself can pair or remove devices."
            assert daemon.pairing.active
            assert summary.id in daemon.remote_devices


async def test_the_computer_removes_a_device_and_drops_its_connection(tmp_path: Path) -> None:
    async with running_daemon(tmp_path) as daemon:
        port = daemon.ws_server.port
        keep, keep_key = daemon.remote_devices.add("Keep")
        gone, gone_key = daemon.remote_devices.add("Gone")
        async with (
            attached(port, daemon.ws_server.token, "/") as window,
            attached(port, gone_key) as gone_ws,
            attached(port, keep_key) as keep_ws,
        ):
            await presence(window)
            both = await presence(window)
            assert {d["id"] for d in both if d["connected"]} == {keep.id, gone.id}
            state = await ask(window, {"type": "remove_remote_device", "device_id": gone.id})
            assert state["type"] == "setup_state"
            assert [d["id"] for d in state["remote_devices"]] == [keep.id]
            await asyncio.wait_for(gone_ws.wait_closed(), 5)
            await asyncio.wait_for(keep_ws.ping(), 5)
            # The close is announced too, after the reply.
            assert [d["id"] for d in await presence(window)] == [keep.id]
            unknown = await ask(window, {"type": "remove_remote_device", "device_id": "dev_nope"})
            assert unknown["type"] == "setup_state"
            assert [d["id"] for d in unknown["remote_devices"]] == [keep.id]
        assert (await hello_reply(port, gone_key))["code"] == "auth_failed"


async def test_cancel_pairing_clears_the_code(tmp_path: Path) -> None:
    async with running_daemon(tmp_path) as daemon:
        port = daemon.ws_server.port
        async with attached(port, daemon.ws_server.token, "/") as window:
            code = (await ask(window, {"type": "start_pairing"}))["code"]
            assert (await ask(window, {"type": "cancel_pairing"}))["type"] == "setup_state"
        reply, _ = await pair(port, code)
        assert reply["code"] == "pair_failed"


async def test_start_pairing_is_refused_at_the_device_limit(tmp_path: Path) -> None:
    async with running_daemon(tmp_path) as daemon:
        for i in range(MAX_DEVICES):
            daemon.remote_devices.add(f"Phone {i}")
        async with attached(daemon.ws_server.port, daemon.ws_server.token, "/") as window:
            reply = await ask(window, {"type": "start_pairing"})
        assert reply["type"] == "error" and reply["code"] == "pairing_limit"
        assert str(MAX_DEVICES) in reply["message"]
        assert not daemon.pairing.active


async def test_remote_attach_off_keeps_paired_devices(tmp_path: Path) -> None:
    async with running_daemon(tmp_path) as daemon:
        port = daemon.ws_server.port
        async with attached(port, daemon.ws_server.token, "/") as window:
            code = (await ask(window, {"type": "start_pairing"}))["code"]
            reply, _ = await pair(port, code)
            await _recv(window)  # pairing_completed
            state = await ask(window, {"type": "set_remote_attach", "enabled": False})
            assert state["remote_attach_enabled"] is False
            assert [d["id"] for d in state["remote_devices"]] == [reply["device_id"]]
            assert (tmp_path / REMOTE_DEVICES_FILE).exists()
            await ask(window, {"type": "set_remote_attach", "enabled": True})
        assert (await hello_reply(port, reply["token"]))["type"] == "hello_ack"


async def test_a_restart_keeps_the_paired_device(tmp_path: Path) -> None:
    async with running_daemon(tmp_path) as daemon:
        summary, key = daemon.remote_devices.add("Phone")
    async with running_daemon(tmp_path) as again:
        assert again.remote_devices.verify(key) == summary.id
        assert (await hello_reply(again.ws_server.port, key))["type"] == "hello_ack"


async def test_a_phone_can_forget_itself_and_nothing_else(tmp_path: Path) -> None:
    async with running_daemon(tmp_path) as daemon:
        port = daemon.ws_server.port
        me, my_key = daemon.remote_devices.add("Me")
        other, _ = daemon.remote_devices.add("Other")
        async with attached(port, daemon.ws_server.token, "/") as window:
            async with attached(port, my_key) as phone:
                await presence(window)
                refused = await ask(phone, {"type": "remove_remote_device", "device_id": other.id})
                assert refused["code"] == "loopback_only"
                state = await ask(phone, {"type": "remove_remote_device", "device_id": me.id})
                assert state["type"] == "setup_state"
                # TD-5015 (SPEC A.11): a remote peer never hears the device list.
                assert "remote_devices" not in state and "remote_port" not in state
                await asyncio.wait_for(phone.wait_closed(), 5)
                assert phone.close_code == 1008
            assert [d["id"] for d in await presence(window)] == [other.id]
        assert other.id in daemon.remote_devices and me.id not in daemon.remote_devices
        assert (await hello_reply(port, my_key))["code"] == "auth_failed"


async def test_a_legacy_token_phone_cannot_remove_anything(tmp_path: Path) -> None:
    async with running_daemon(tmp_path) as daemon:
        summary, _ = daemon.remote_devices.add("Phone")
        async with attached(daemon.ws_server.port, daemon.ws_server.remote_token) as phone:
            reply = await ask(phone, {"type": "remove_remote_device", "device_id": summary.id})
        assert reply["code"] == "loopback_only"
        assert summary.id in daemon.remote_devices


async def test_a_failed_remove_says_so_and_still_drops_the_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(errno.EACCES, "Permission denied")

    async with running_daemon(tmp_path) as daemon:
        port = daemon.ws_server.port
        summary, key = daemon.remote_devices.add("Phone")
        async with (
            attached(port, daemon.ws_server.token, "/") as window,
            attached(port, key) as phone,
        ):
            await presence(window)
            monkeypatch.setattr("tstd.remote_devices.os.replace", denied)
            reply = await ask(window, {"type": "remove_remote_device", "device_id": summary.id})
            assert reply == {
                "type": "error",
                "code": "remove_failed",
                "message": "Couldn't update the paired devices file: Permission denied",
            }
            await asyncio.wait_for(phone.wait_closed(), 5)
            assert [d["connected"] for d in await presence(window)] == [False]
        # Still in the file, so still in memory: the two never disagree.
        assert daemon.remote_devices.verify(key) == summary.id
