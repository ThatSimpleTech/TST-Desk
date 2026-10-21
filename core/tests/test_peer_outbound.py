"""Another computer never learns where this computer's models live (TD-5169).

A paired machine gets ``tier_state`` without ``hosts`` and
``model_fallback`` without its hosts; a phone and this computer keep them.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tests.platform_helpers import stop_daemon_gracefully
from tstd import daemon as daemon_module
from tstd.daemon import Daemon
from tstd.peer_policy import (
    HOST_PLACEHOLDER,
    LOOPBACK_PLAIN,
    PeerView,
    outbound_event,
    service_hosts,
)
from tstd.protocol import PROTOCOL_VERSION, AssistantDelta, ModelFallback, TierState

MACHINE = PeerView(remote=True, machine=True, viewer=False)
SELF_PAIRED = PeerView(remote=False, machine=True, viewer=False)
PHONE = PeerView(remote=True, machine=False, viewer=True)

TIER = TierState(
    session_id="s1",
    tier="brain",
    model_slugs={"brain": "gateway-model"},
    hosts={"brain": "192.0.2.49:4000"},
    seq=3,
)
FALLBACK = ModelFallback(
    session_id="s1",
    tier="worker",
    from_model="a",
    from_host="192.0.2.49:4000",
    to_model="b",
    to_host="192.0.2.50:8000",
    fallback=1,
    reason="connection_refused",
    seq=4,
)


@pytest.mark.parametrize("peer", [MACHINE, SELF_PAIRED])
def test_machine_peer_gets_no_hosts(peer: PeerView) -> None:
    tier = outbound_event(TIER, peer)
    assert isinstance(tier, TierState)
    assert tier.hosts == {}
    assert tier.model_slugs == {"brain": "gateway-model"}
    assert tier.seq == 3
    fallback = outbound_event(FALLBACK, peer)
    assert isinstance(fallback, ModelFallback)
    assert (fallback.from_host, fallback.to_host) == ("", "")
    assert (fallback.from_model, fallback.to_model) == ("a", "b")
    # The logged event itself is untouched.
    assert TIER.hosts == {"brain": "192.0.2.49:4000"}


@pytest.mark.parametrize("peer", [LOOPBACK_PLAIN, PHONE])
def test_phone_and_this_computer_keep_hosts(peer: PeerView) -> None:
    assert outbound_event(TIER, peer) is TIER
    assert outbound_event(FALLBACK, peer) is FALLBACK


def test_other_events_pass_through_for_a_machine() -> None:
    delta = AssistantDelta(session_id="s1", delta="192.0.2.49", seq=5)
    assert outbound_event(delta, MACHINE) is delta


def test_service_hosts_walks_every_base_url() -> None:
    dump = {
        "tiers": {
            "brain": {"base_url": "http://192.0.2.49:4000/v1", "model": "m"},
            "worker": {"base_url": "http://127.0.0.1:8000/v1"},
        },
        "fallbacks": [{"base_url": "https://box.example.ts.net/v1"}],
        "credentials": {"k": {"base_url": None}, "j": {"voice_base_url": "gpu.lan:9000"}},
    }
    assert service_hosts(dump) == {
        "192.0.2.49:4000",
        "192.0.2.49",
        "box.example.ts.net",
        "gpu.lan:9000",
        "gpu.lan",
    }


def test_machine_peer_text_has_hosts_replaced() -> None:
    hosts = service_hosts({"base_url": "http://192.0.2.49:4000/v1"})
    delta = AssistantDelta(
        session_id="s1", delta="Failed to connect to http://192.0.2.49:4000/v1: refused", seq=5
    )
    out = outbound_event(delta, MACHINE, hosts)
    assert isinstance(out, AssistantDelta)
    assert out.delta == f"Failed to connect to http://{HOST_PLACEHOLDER}/v1: refused"
    assert outbound_event(delta, PHONE, hosts) is delta
    assert outbound_event(TIER, MACHINE, hosts).hosts == {}  # type: ignore[attr-defined]


def test_service_hosts_include_fallback_base_urls() -> None:
    dump = {"tiers": {"brain": {"fallback_base_urls": ["http://192.0.2.60:8000/v1", ""]}}}
    assert service_hosts(dump) == {"192.0.2.60:8000", "192.0.2.60"}


def test_only_whole_host_names_are_replaced_and_keys_never() -> None:
    """A host inside a longer address, a longer name, or a JSON key stays as it is."""
    hosts = service_hosts({"base_url": "http://192.0.2.5:4000/v1", "x_base_url": "http://gpu"})
    text = "192.0.2.50 and gpu-box and gpu.lan, but 192.0.2.5 and GPU fail"
    delta = AssistantDelta(session_id="s1", delta=text, seq=5)
    out = outbound_event(delta, MACHINE, hosts)
    assert isinstance(out, AssistantDelta)
    assert out.delta == (
        f"192.0.2.50 and gpu-box and gpu.lan, but {HOST_PLACEHOLDER} and {HOST_PLACEHOLDER} fail"
    )
    # A host that is also the text of a field name (here "delta") cannot break the event.
    named = outbound_event(delta, MACHINE, frozenset({"delta"}))
    assert isinstance(named, AssistantDelta) and named.delta == text


async def _hello(uri: str, token: str) -> Any:
    ws = await connect(uri)
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    assert json.loads(await ws.recv())["type"] == "hello_ack"
    return ws


_seen: list[dict[str, Any]] = []


async def _tier_states(ws: Any, count: int) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    while len(seen) < count:
        evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        _seen.append(evt)
        if evt["type"] == "tier_state":
            seen.append(evt)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize(("peer", "stripped"), [(MACHINE, True), (LOOPBACK_PLAIN, False)])
async def test_attach_replay_and_live_stream(
    tmp_path: Path, peer: PeerView, stripped: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seen.clear()
    data_dir = tmp_path / "data"
    workspace = tmp_path / "ws"
    data_dir.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(
        daemon_module, "service_hosts", lambda _dump: frozenset({"192.0.2.49:4000"})
    )
    daemon = Daemon(data_dir=data_dir)
    task = asyncio.create_task(daemon.run())
    try:
        for _ in range(50):
            if daemon.ws_server.port:
                break
            await asyncio.sleep(0.05)
        ws = await _hello(f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token)
        await ws.send(json.dumps({"type": "open_workspace", "path": str(workspace)}))
        session_id = json.loads(await ws.recv())["session_id"]
        session = daemon.session_registry.get(session_id)
        assert session is not None
        await session.event_log.add(TIER.model_copy(update={"session_id": session_id}))

        # Only the attach path sees the peer kind under test.
        daemon.ws_server.peer_view = lambda _conn: peer  # type: ignore[method-assign]
        await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 1}))
        replayed = await _tier_states(ws, 2)  # the one logged at open, then ours
        await session.event_log.add(
            AssistantDelta(session_id=session_id, delta="down: 192.0.2.49:4000", seq=1)
        )
        await session.event_log.add(TIER.model_copy(update={"session_id": session_id}))
        (live,) = await _tier_states(ws, 1)
        delta = next(e for e in _seen if e["type"] == "assistant_delta")
        assert delta["delta"] == ("down: model-service" if stripped else "down: 192.0.2.49:4000")

        ours = [replayed[-1], live]
        for evt in ours:
            assert evt["model_slugs"] == {"brain": "gateway-model"}
            assert evt["hosts"] == ({} if stripped else {"brain": "192.0.2.49:4000"})
        await ws.close()
    finally:
        await stop_daemon_gracefully(daemon, task)
