"""TD-5015: pairing with another computer, as a phone would but kind=machine.

The other computer here is a real :class:`WebSocketServer` on
127.0.0.1 whose ``/phone`` path counts as remote; the hub's dial rule is
the test one. Production's rule (Tailscale only) is checked on its own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.machine_helpers import LOCAL, Fleet, eventually, is_phone
from tests.test_remote_devices import FakeClock
from tstd.daemon import Daemon
from tstd.machine_pairing import (
    NOT_TAILSCALE_MESSAGE,
    DialPolicy,
    PairError,
    PairTarget,
    checked_url,
    dial,
    dial_allowed,
    hello_frame,
    parse_target,
    redeem,
    remove_self,
    ws_url,
)
from tstd.remote_devices import MAX_FAILED_REDEEMS, PairingCodes, RemoteDeviceRegistry
from tstd.ws import WebSocketServer

# ── the Tailscale rule ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    [
        "100.64.0.1",
        "100.127.255.254",
        "fd7a:115c:a1e0::1",
        "[fd7a:115c:a1e0::1]",
        "ezer.tail1.ts.net",
    ],
)
def test_tailscale_hosts_may_be_dialed(host: str) -> None:
    assert dial_allowed(host)


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "192.168.1.4", "100.128.0.1", "::1", "ts.net", "x.ts.net.evil.com", ""],
)
def test_other_hosts_may_not(host: str) -> None:
    assert not dial_allowed(host)


# Names that end in ".ts.net" but whose URL would open another host.
URL_TRICKS = [
    "evil.com/a.ts.net",
    "evil.com:443/x.ts.net",
    "evil.com?.ts.net",
    "a@evil.com#.ts.net",
    "evil.com#.ts.net",
    "evil com.tail.ts.net",
    "-ezer.tail.ts.net",
    "fd7a:115c:a1e0::1%@evil.com",
]


@pytest.mark.parametrize("host", URL_TRICKS)
def test_a_name_with_url_syntax_is_not_a_tailscale_name(host: str) -> None:
    assert not dial_allowed(host)


@pytest.mark.parametrize("host", URL_TRICKS)
def test_a_link_or_typed_address_with_url_syntax_is_refused(host: str) -> None:
    link = f"tstdesk://pair?h={quote(host, safe='')}&p=39079&c=ABCD2345"
    for kwargs in ({"link": link}, {"host": host, "port": 39079, "code": "ABCD2345"}):
        with pytest.raises(PairError) as raised:
            parse_target(**kwargs)  # type: ignore[arg-type]
        assert raised.value.code == "not_tailscale"


@pytest.mark.parametrize("host", URL_TRICKS[:5])
async def test_the_dial_url_must_name_exactly_the_checked_host(host: str) -> None:
    # Even under a rule that allowed it, the socket never opens elsewhere.
    with pytest.raises(PairError) as raised:
        await dial(host, 39079, DialPolicy(allowed=lambda _host: True))
    assert raised.value.code == "not_tailscale"
    assert checked_url("ezer.tail1.ts.net", 39079) == "ws://ezer.tail1.ts.net:39079"
    assert checked_url("[fd7a:115c:a1e0::1]", 1, "/phone") == "ws://[fd7a:115c:a1e0::1]:1/phone"


async def test_dial_checks_the_rule_every_time() -> None:
    with pytest.raises(PairError) as raised:
        await dial("127.0.0.1", 1, DialPolicy())
    assert raised.value.code == "not_tailscale"


def test_ws_url_brackets_ipv6() -> None:
    assert ws_url("fd7a:115c:a1e0::1", 39079) == "ws://[fd7a:115c:a1e0::1]:39079"
    assert ws_url("100.64.0.1", 1, "/phone") == "ws://100.64.0.1:1/phone"


def test_the_hello_says_bridge() -> None:
    assert '"features": ["bridge"]' in hello_frame("k")


# ── the two forms ────────────────────────────────────────────────────


def test_a_link_gives_host_port_code_and_name() -> None:
    target = parse_target(link="tstdesk://pair?h=100.64.1.5&p=39079&c=abcd-2345&n=ezer")
    assert target == PairTarget(host="100.64.1.5", port=39079, code="ABCD2345", name="ezer")


def test_a_typed_address_and_code() -> None:
    target = parse_target(host=" [fd7a:115c:a1e0::9] ", port=39080, code="abcd 2345")
    assert target == PairTarget(host="fd7a:115c:a1e0::9", port=39080, code="ABCD2345")


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"link": "tstdesk://pair?h=192.168.1.4&p=39079&c=ABCD2345"}, "not_tailscale"),
        ({"link": "https://pair?h=100.64.1.5&p=1&c=ABCD2345"}, "pair_failed"),
        ({"link": "tstdesk://pair?h=100.64.1.5&c=ABCD2345"}, "pair_failed"),
        ({"host": "100.64.1.5", "port": 39079, "code": "short"}, "pair_failed"),
        ({"host": "100.64.1.5", "port": 0, "code": "ABCD2345"}, "pair_failed"),
        ({"host": "", "port": 39079, "code": "ABCD2345"}, "pair_failed"),
    ],
)
def test_bad_targets_are_refused(kwargs: dict[str, object], code: str) -> None:
    with pytest.raises(PairError) as raised:
        parse_target(**kwargs)  # type: ignore[arg-type]
    assert raised.value.code == code
    if code == "not_tailscale":
        assert raised.value.message == NOT_TAILSCALE_MESSAGE


# ── against a real other computer ────────────────────────────────────


class _Other:
    def __init__(self, tmp: Path) -> None:
        self.clock = FakeClock()
        self.devices = RemoteDeviceRegistry(tmp, clock=self.clock)
        self.devices.load()
        self.pairing = PairingCodes(clock=self.clock)
        self.ws = WebSocketServer(
            tmp,
            ping_interval=0,
            is_remote_connection=is_phone,
            devices=self.devices,
            pairing=self.pairing,
        )

    def target(self, code: str) -> PairTarget:
        return PairTarget(host="127.0.0.1", port=self.ws.port, code=code)


@pytest.fixture
async def other(tmp_path: Path) -> AsyncIterator[_Other]:
    srv = _Other(tmp_path)
    await srv.ws.start()
    try:
        yield srv
    finally:
        await srv.ws.stop()


async def test_redeem_pairs_as_a_machine_and_returns_the_key(other: _Other) -> None:
    code, _ = other.pairing.start()
    outcome = await redeem(other.target(code), device_name="desk", policy=LOCAL)
    assert outcome.machine_id == other.ws.machine_id
    assert outcome.desktop_name and outcome.device_id.startswith("dev_")
    assert other.devices.kind_of(outcome.device_id) == "machine"
    # Bools, so a failure never prints the key.
    works = other.devices.verify(outcome.token) == outcome.device_id
    shown = outcome.token in repr(outcome)
    assert works and not shown


async def test_a_wrong_code_is_pair_failed_and_then_pair_locked(other: _Other) -> None:
    other.pairing.start()
    with pytest.raises(PairError) as raised:
        await redeem(other.target("ZZZZ9999"), device_name="desk", policy=LOCAL)
    assert raised.value.code == "pair_failed"
    for _ in range(MAX_FAILED_REDEEMS):
        with pytest.raises(PairError) as raised:
            await redeem(other.target("ZZZZ9999"), device_name="desk", policy=LOCAL)
    assert raised.value.code == "pair_locked"
    assert len(other.devices) == 0


async def test_nobody_listening_is_machine_unreachable() -> None:
    target = PairTarget(host="127.0.0.1", port=1, code="ABCD2345")
    with pytest.raises(PairError) as raised:
        await redeem(target, device_name="desk", policy=LOCAL, wait_seconds=2)
    assert raised.value.code == "machine_unreachable"


async def test_replaces_retires_the_old_entry_there(other: _Other) -> None:
    code, _ = other.pairing.start()
    first = await redeem(other.target(code), device_name="desk", policy=LOCAL)
    code, _ = other.pairing.start()
    second = await redeem(
        other.target(code), device_name="desk", replaces=first.token, policy=LOCAL
    )
    # Retired after the close; a key is never part of an assertion message.
    await eventually(lambda: other.devices.kind_of(first.device_id) is None)
    assert other.devices.kind_of(second.device_id) == "machine"
    assert len(other.devices) == 1


# ── self-removal, against a real daemon (the gate lives there) ───────


@pytest.fixture
async def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Daemon]:
    fleet = Fleet(monkeypatch)
    try:
        yield await fleet.start(tmp_path / "b")
    finally:
        await fleet.close()


async def _paired(daemon: Daemon) -> tuple[str, str]:
    """A hub entry on *daemon*: (device id, key), redeemed over the socket."""
    code, _ = daemon.pairing.start()
    target = PairTarget(host="127.0.0.1", port=daemon.ws_server.port, code=code)
    outcome = await redeem(target, device_name="desk", policy=LOCAL)
    return outcome.device_id, outcome.token


async def test_remove_self_drops_only_this_hub(daemon: Daemon) -> None:
    phone = daemon.remote_devices.add("Pixel")[0]
    device_id, key = await _paired(daemon)
    port = daemon.ws_server.port
    gone = await remove_self("127.0.0.1", port, key, device_id, policy=LOCAL)
    assert gone is True
    assert daemon.remote_devices.kind_of(device_id) is None
    assert daemon.remote_devices.kind_of(phone.id) == "phone"
    # A key the other side forgot is a quiet False.
    again = await remove_self("127.0.0.1", port, key, device_id, policy=LOCAL)
    assert again is False


async def test_remove_self_cannot_name_another_device(daemon: Daemon) -> None:
    phone = daemon.remote_devices.add("Pixel")[0]
    _, key = await _paired(daemon)
    port = daemon.ws_server.port
    removed = await remove_self("127.0.0.1", port, key, phone.id, policy=LOCAL)
    assert removed is False
    assert daemon.remote_devices.kind_of(phone.id) == "phone"
