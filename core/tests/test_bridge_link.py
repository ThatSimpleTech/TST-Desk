"""TD-5015: the kept-up socket to a paired computer.

The other computer is a real :class:`WebSocketServer` on 127.0.0.1
(``/phone`` counts as remote). Backoff and the 60 s "unreachable" mark
run on an injected sleep that moves an injected clock, so nothing here
waits for real.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from tests.machine_helpers import LOCAL, Clock, eventually, is_phone
from tests.test_remote_devices import FakeClock
from tstd.bridge_link import (
    BACKOFF_CAP_SECONDS,
    LOCKED_MESSAGE,
    MISSING_KEY_MESSAGE,
    REFUSED_MESSAGE,
    UpstreamLink,
    backoff_delay,
)
from tstd.keychain import KeychainError, KeychainLockedError
from tstd.machine_pairing import NOT_TAILSCALE_MESSAGE, DialPolicy
from tstd.remote_devices import PairingCodes, RemoteDeviceRegistry
from tstd.ws import WebSocketServer


class _Other:
    def __init__(self, tmp: Path, ping_interval: float = 0) -> None:
        self.devices = RemoteDeviceRegistry(tmp, clock=FakeClock())
        self.devices.load()
        self.ws = WebSocketServer(
            tmp,
            ping_interval=ping_interval,
            is_remote_connection=is_phone,
            devices=self.devices,
            pairing=PairingCodes(),
        )


@pytest.fixture
async def other(tmp_path: Path) -> AsyncIterator[_Other]:
    srv = _Other(tmp_path)
    await srv.ws.start()
    try:
        yield srv
    finally:
        await srv.ws.stop()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


class _Probe:
    """Records what the link reports."""

    def __init__(self) -> None:
        self.opened = 0
        self.frames: list[dict[str, Any]] = []
        self.statuses: list[str] = []
        self.seen: list[bool] = []

    async def on_open(self) -> None:
        self.opened += 1

    async def on_frame(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)


def _link(
    port: int,
    key: Callable[[], Any],
    probe: _Probe,
    *,
    policy: DialPolicy = LOCAL,
    clock: Clock | None = None,
    sleep: Any = asyncio.sleep,
    silence: float = 45.0,
) -> UpstreamLink:
    link: UpstreamLink

    def status() -> None:
        probe.statuses.append(link.status)

    link = UpstreamLink(
        label="mch_0123456789ab/test",
        host="127.0.0.1",
        port=port,
        key=key,
        policy=policy,
        on_open=probe.on_open,
        on_frame=probe.on_frame,
        on_status=status,
        on_seen=probe.seen.append,
        name="tower",
        clock=clock or Clock(),
        jitter=lambda: 0.0,
        sleep=sleep,
        silence=silence,
    )
    return link


def _key(value: str) -> Callable[[], Any]:
    async def get() -> str:
        return value

    return get


async def _short_sleep(seconds: float) -> None:
    await asyncio.sleep(min(seconds, 0.01))


def _hanging_dial(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """Every dial from here on hangs (a black-holed host) until the event is set."""
    hang = asyncio.Event()

    async def dial(*_args: Any) -> Any:
        await hang.wait()
        raise OSError("timed out")

    monkeypatch.setattr("tstd.bridge_link.dial", dial)
    return hang


def _fake_sleep(clock: Clock) -> Any:
    async def sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    return sleep


# ── backoff ──────────────────────────────────────────────────────────


def test_backoff_doubles_and_caps_at_30_with_jitter_taking_at_most_a_fifth() -> None:
    assert [backoff_delay(i, 0.0) for i in range(7)] == [1, 2, 4, 8, 16, 30, 30]
    assert backoff_delay(100, 0.0) == BACKOFF_CAP_SECONDS
    assert backoff_delay(3, 0.999) == pytest.approx(8 * 0.8, rel=0.01)
    assert all(backoff_delay(i, 0.999) <= BACKOFF_CAP_SECONDS for i in range(40))


async def test_nobody_there_backs_off_and_turns_unreachable_after_60_s() -> None:
    clock = Clock()
    probe = _Probe()
    link = _link(_free_port(), _key("k" * 64), probe, clock=clock, sleep=_fake_sleep(clock))
    link.start()
    try:
        await eventually(lambda: len(link.delays) >= 8, within=10)
    finally:
        await link.stop()
    assert link.delays[:8] == [1, 2, 4, 8, 16, 30, 30, 30]
    # Every sleep here moves the clock, so 60 s pass at once: unreachable, once.
    assert probe.statuses[0] == "unreachable" and probe.statuses.count("unreachable") == 1
    assert link.status == "unreachable" and probe.opened == 0


async def test_a_black_holed_host_turns_unreachable_at_60_s_mid_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    _hanging_dial(monkeypatch)
    link = _link(1, _key("k" * 64), _Probe(), clock=clock, sleep=_short_sleep)
    link.start()
    try:
        await asyncio.sleep(0.05)
        clock.advance(59.5)
        await asyncio.sleep(0.05)
        assert link.status == "connecting"
        clock.advance(0.5)
        await eventually(lambda: link.status == "unreachable", within=2)
        assert link.delays == []  # still the first dial
    finally:
        await link.stop()


async def test_after_a_drop_unreachable_comes_60_s_later_even_mid_dial(
    other: _Other, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    _, key = other.devices.add("hub", kind="machine")
    link = _link(other.ws.port, _key(key), _Probe(), clock=clock, sleep=_short_sleep)
    link.start()
    try:
        await eventually(lambda: link.is_open)
        clock.advance(600)  # a long healthy stretch counts for nothing
        _hanging_dial(monkeypatch)
        for conn in list(other.ws._connections):
            await conn.close(1001)
        await eventually(lambda: link.status == "connecting")
        clock.advance(59.5)
        await asyncio.sleep(0.05)
        assert link.status == "connecting"
        clock.advance(0.5)
        await eventually(lambda: link.status == "unreachable", within=2)
    finally:
        await link.stop()


async def test_try_now_during_a_dial_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    gate = asyncio.Event()

    async def dial(*_args: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            await gate.wait()
        raise OSError("refused")

    monkeypatch.setattr("tstd.bridge_link.dial", dial)
    stalled = asyncio.Event()

    async def sleep(_seconds: float) -> None:
        await stalled.wait()  # only a wake ends a wait

    link = _link(1, _key("k" * 64), _Probe(), sleep=sleep)
    link.start()
    try:
        await eventually(lambda: calls == 1)
        link.reset_backoff()  # "Try now" while the first dial hangs
        gate.set()
        await eventually(lambda: calls == 2)
    finally:
        await link.stop()


async def test_try_now_skips_the_wait(other: _Other) -> None:
    clock = Clock()
    probe = _Probe()
    _, key = other.devices.add("hub", kind="machine")
    stalled = asyncio.Event()

    async def sleep(_seconds: float) -> None:
        await stalled.wait()

    port = _free_port()
    link = _link(port, _key(key), probe, clock=clock, sleep=sleep)
    link.start()
    try:
        await eventually(lambda: len(link.delays) == 1)
        link.port = other.ws.port
        link.reset_backoff()
        await eventually(lambda: link.is_open)
    finally:
        await link.stop()


# ── connected ────────────────────────────────────────────────────────


async def test_a_good_key_connects_and_learns_who_is_there(other: _Other) -> None:
    _, key = other.devices.add("hub", kind="machine")
    probe = _Probe()
    link = _link(other.ws.port, _key(key), probe)
    link.start()
    try:
        await eventually(lambda: link.is_open and probe.opened == 1)
        assert link.status == "connected" and probe.statuses == ["connected"]
        assert link.remote_machine_id == other.ws.machine_id
        assert link.send('{"type": "list_sessions"}')
        assert probe.seen and probe.seen[-1] is False
    finally:
        await link.stop()
    assert not link.send('{"type": "list_sessions"}')


async def test_a_drop_reports_last_seen_and_reconnects(other: _Other) -> None:
    device, key = other.devices.add("hub", kind="machine")
    probe = _Probe()
    link = _link(other.ws.port, _key(key), probe)
    link.start()
    try:
        await eventually(lambda: probe.opened == 1)
        for conn in list(other.ws._connections):
            await conn.close(1001)
        await eventually(lambda: True in probe.seen)
        await eventually(lambda: probe.opened == 2, within=5)
        assert link.delays == [1.0]
        assert other.devices.kind_of(device.id) == "machine"
    finally:
        await link.stop()


async def test_silence_forces_a_reconnect(other: _Other) -> None:
    _, key = other.devices.add("hub", kind="machine")
    probe = _Probe()
    link = _link(other.ws.port, _key(key), probe, silence=0.2)
    link.start()
    try:
        # No pings from this server: 0.2 s of silence ends each socket.
        await eventually(lambda: probe.opened >= 2, within=5)
    finally:
        await link.stop()


# ── refused and parked ───────────────────────────────────────────────


async def test_a_wrong_key_is_refused_and_stops_dialing(other: _Other) -> None:
    probe = _Probe()
    link = _link(other.ws.port, _key("f" * 64), probe)
    link.start()
    try:
        await eventually(lambda: link.status == "refused")
        assert link.detail == REFUSED_MESSAGE.format(name="tower")
        await asyncio.sleep(0.3)
        assert link.delays == [] and probe.opened == 0 and link.running
    finally:
        await link.stop()


async def test_a_removed_device_close_1008_is_refused(other: _Other) -> None:
    device, key = other.devices.add("hub", kind="machine")
    probe = _Probe()
    link = _link(other.ws.port, _key(key), probe)
    link.start()
    try:
        await eventually(lambda: link.is_open)
        other.devices.remove(device.id)
        await other.ws.close_device(device.id)
        await eventually(lambda: link.status == "refused")
        assert link.delays == []
    finally:
        await link.stop()


async def test_a_locked_keychain_parks_as_locked_and_a_missing_key_as_refused() -> None:
    async def locked() -> str:
        raise KeychainLockedError("locked")

    async def missing() -> str:
        raise KeychainError("not found")

    for get, status, detail in (
        (locked, "locked", LOCKED_MESSAGE.format(name="tower")),
        (missing, "refused", MISSING_KEY_MESSAGE.format(name="tower")),
    ):
        probe = _Probe()
        link = _link(_free_port(), get, probe)
        link.start()
        try:
            await eventually(lambda link=link, status=status: link.status == status)  # type: ignore[misc]
            assert link.detail == detail and link.delays == []
        finally:
            await link.stop()


async def test_a_viewer_nudge_never_wakes_a_parked_link_but_try_now_does(other: _Other) -> None:
    reads = {"n": 0}

    async def locked() -> str:
        reads["n"] += 1
        raise KeychainLockedError("locked")

    async def wrong() -> str:
        reads["n"] += 1
        return "f" * 64

    for get, port, status in (
        (locked, _free_port(), "locked"),
        (wrong, other.ws.port, "refused"),
    ):
        reads["n"] = 0
        link = _link(port, get, _Probe())
        link.start()
        try:
            await eventually(lambda link=link, status=status: link.status == status)  # type: ignore[misc]
            for _ in range(5):
                link.nudge()
                await asyncio.sleep(0.02)
            assert reads["n"] == 1, status  # no keychain prompt, no revoked-key dial
            link.reset_backoff()
            await eventually(lambda: reads["n"] == 2)
        finally:
            await link.stop()


async def test_a_host_off_tailscale_is_never_dialed(other: _Other) -> None:
    _, key = other.devices.add("hub", kind="machine")
    probe = _Probe()
    link = _link(other.ws.port, _key(key), probe, policy=DialPolicy())
    link.start()
    try:
        await eventually(lambda: link.status == "refused")
        assert link.detail == NOT_TAILSCALE_MESSAGE
        assert other.ws.client_count == 0 and probe.opened == 0
    finally:
        await link.stop()
