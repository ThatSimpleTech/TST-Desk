"""TD-5015: who a connection is, what it may send, and what it hears.

Pure checks walk every client message type, so a verb added later starts
refused for another computer's hub. The socket checks run a real daemon:
a machine peer lists own rows only and hears no broadcast, a remote
peer's ``setup_state`` has no devices or address, ``focus_window`` stays
on this computer and ``job_list`` goes to viewers.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import ClientConnection

from tests.machine_helpers import Fleet, ask, recv, recv_until, window
from tstd import protocol
from tstd.daemon import Daemon
from tstd.hub import REMOTE_SETUP_STATE_DROPS
from tstd.peer_policy import (
    APPROVAL_VERBS,
    FORWARDED_VERBS,
    LOOPBACK_PLAIN,
    LOOPBACK_VERBS,
    MACHINE_PEER_VERBS,
    MACHINE_VERBS,
    PeerView,
    check,
    classify,
)
from tstd.protocol import parse_client_message

# Checked one by one for a way to switch approvals off, widen a rule, or
# reach this computer's own settings, devices, files or screen.
NEVER_FOR_MACHINES = frozenset(
    {
        "set_skip_all_approvals",
        "set_grok_mode",
        "approve_grok_plan",
        "start_autonomy",
        "revoke_policy_rule",
        "list_policy_rules",
        "set_api_key",
        "delete_api_key",
        "set_credential",
        "delete_credential",
        "set_remote_attach",
        "rotate_remote_token",
        "start_pairing",
        "cancel_pairing",
        "add_machine",
        "remove_machine",
        "list_machines",
        "reconnect_machine",
        "open_workspace",
        "shutdown",
        "drain",
        "save_job",
        "run_job",
        "set_cu_kill",
        "set_cu_policy",
    }
)

VIEWER = PeerView(remote=False, machine=False, viewer=True)
PHONE_VIEWER = PeerView(remote=True, machine=False, viewer=True)
MACHINE = PeerView(remote=True, machine=True, viewer=False)
PLAIN_REMOTE = PeerView(remote=True, machine=False, viewer=False)
BRIDGED = "s1:mch_0123456789ab"


def _msg(kind: str, **fields: object) -> protocol.ClientMessage:
    return parse_client_message(json.dumps({"type": kind, **fields}))


# ── classify ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("remote", "kind", "features", "expected"),
    [
        (False, None, ["machines"], VIEWER),
        (False, None, [], LOOPBACK_PLAIN),
        (True, "phone", ["machines"], PHONE_VIEWER),
        (True, None, ["machines"], PLAIN_REMOTE),  # the legacy token is no device
        (True, "phone", [], PLAIN_REMOTE),
        (True, "machine", ["machines"], MACHINE),
        (True, "phone", ["machines", "bridge"], MACHINE),
        (False, None, ["bridge"], PeerView(remote=False, machine=True, viewer=False)),
    ],
)
def test_classify(remote: bool, kind: object, features: list[str], expected: PeerView) -> None:
    assert classify(remote, kind, features) == expected  # type: ignore[arg-type]


# ── the verb sets ────────────────────────────────────────────────────


def test_the_machine_peer_set_is_exactly_the_spec_list() -> None:
    assert MACHINE_PEER_VERBS <= protocol._KNOWN_CLIENT_TYPES
    assert MACHINE_PEER_VERBS - {"remove_remote_device"} == FORWARDED_VERBS
    assert APPROVAL_VERBS <= FORWARDED_VERBS
    assert MACHINE_VERBS.isdisjoint(MACHINE_PEER_VERBS)
    assert NEVER_FOR_MACHINES <= protocol._KNOWN_CLIENT_TYPES
    assert NEVER_FOR_MACHINES.isdisjoint(MACHINE_PEER_VERBS)


def test_every_client_type_not_listed_is_refused_for_a_machine_peer() -> None:
    """Deny by default: walks every known client type."""
    allowed = []
    for kind in sorted(protocol._KNOWN_CLIENT_TYPES - {"hello", "pair"}):
        fake = type("Fake", (), {"type": kind, "session_id": "s1"})()
        refusal = check(fake, MACHINE, bridged=False)  # type: ignore[arg-type]
        if refusal is None:
            allowed.append(kind)
        else:
            expected = "loopback_only" if kind in LOOPBACK_VERBS else "not_allowed"
            assert json.loads(refusal)["code"] == expected, kind
            assert json.loads(refusal)["in_reply_to"] == kind  # TD-5144
    assert set(allowed) == MACHINE_PEER_VERBS


def test_local_server_scan_is_loopback_only() -> None:
    """TD-5180: only the person at this computer starts a probe of its ports."""
    scan = _msg("scan_local_servers")
    assert check(scan, LOOPBACK_PLAIN, bridged=False) is None
    assert check(scan, VIEWER, bridged=False) is None
    for peer in (PHONE_VIEWER, PLAIN_REMOTE, MACHINE):
        refusal = check(scan, peer, bridged=False)
        assert refusal is not None and json.loads(refusal)["code"] == "loopback_only", peer
        assert json.loads(refusal)["in_reply_to"] == "scan_local_servers"


def test_drain_is_loopback_only() -> None:
    """TD-5023: a phone or another computer never holds this computer's turns."""
    assert "drain" in protocol._KNOWN_CLIENT_TYPES
    assert LOOPBACK_VERBS.isdisjoint(MACHINE_PEER_VERBS | FORWARDED_VERBS)
    drain = _msg("drain", timeout_s=5, hold_s=5)
    assert check(drain, LOOPBACK_PLAIN, bridged=False) is None
    assert check(drain, VIEWER, bridged=False) is None
    self_paired = PeerView(remote=False, machine=True, viewer=False)
    for peer in (PHONE_VIEWER, PLAIN_REMOTE, MACHINE, self_paired):
        refusal = check(drain, peer, bridged=False)
        assert refusal is not None and json.loads(refusal)["code"] == "loopback_only", peer
        assert json.loads(refusal)["in_reply_to"] == "drain"


def test_a_machine_peer_never_reaches_a_bridged_id() -> None:
    refusal = check(_msg("attach", session_id=BRIDGED), MACHINE, bridged=True)
    assert refusal is not None
    assert json.loads(refusal) == {
        "type": "error",
        "code": "bridged_session",
        "message": "That session is on another computer. Open it from the computer it runs on.",
        "session_id": BRIDGED,
        "in_reply_to": "attach",
    }


def test_machine_verbs_are_loopback_only_except_listing_for_viewers() -> None:
    add = _msg("add_machine", host="100.64.0.9", port=39079, code="ABCD2345")
    assert check(add, LOOPBACK_PLAIN, bridged=False) is None
    for peer in (PHONE_VIEWER, PLAIN_REMOTE):
        refusal = check(add, peer, bridged=False)
        assert refusal is not None and json.loads(refusal)["code"] == "loopback_only"
        assert json.loads(refusal)["in_reply_to"] == "add_machine"
    listing = _msg("list_machines")
    assert check(listing, PHONE_VIEWER, bridged=False) is None
    assert check(listing, PLAIN_REMOTE, bridged=False) is not None


def test_a_viewer_gets_only_forwarded_verbs_on_a_bridged_id() -> None:
    assert (
        check(_msg("user_message", session_id=BRIDGED, content="x"), VIEWER, bridged=True) is None
    )
    refusal = check(_msg("list_policy_rules", session_id=BRIDGED), VIEWER, bridged=True)
    assert refusal is not None and json.loads(refusal)["code"] == "not_on_remote"
    assert json.loads(refusal)["session_id"] == BRIDGED
    assert json.loads(refusal)["in_reply_to"] == "list_policy_rules"


def test_read_artifact_chunk_forwarded() -> None:
    """TD-5143: a sent file downloads from a bridged session through the hub."""
    aid = "0" * 32
    bridged = _msg("read_artifact_chunk", session_id=BRIDGED, artifact_id=aid, offset=0)
    own = _msg("read_artifact_chunk", session_id="s1", artifact_id=aid, offset=0)
    assert "read_artifact_chunk" in FORWARDED_VERBS
    for viewer in (VIEWER, PHONE_VIEWER):
        assert check(bridged, viewer, bridged=True) is None
        # The phone pulling an APK from this computer's own session.
        assert check(own, viewer, bridged=False) is None
    # The hub's forwarded request reaches the other computer as a machine peer.
    assert check(own, MACHINE, bridged=False) is None
    assert check(own, PLAIN_REMOTE, bridged=False) is None
    second_hop = check(bridged, MACHINE, bridged=True)
    assert second_hop is not None and json.loads(second_hop)["code"] == "bridged_session"
    assert json.loads(second_hop)["in_reply_to"] == "read_artifact_chunk"


# ── on a real daemon ─────────────────────────────────────────────────


@pytest.fixture
async def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Daemon]:
    fleet = Fleet(monkeypatch)
    try:
        yield await fleet.start(tmp_path / "a")
    finally:
        await fleet.close()


async def _next(ws: ClientConnection, within: float = 5.0) -> dict[str, Any]:
    """The next frame that is not a presence ``setup_state``."""
    return await recv_until(ws, lambda f: f["type"] != "setup_state", within)


def _machine_key(daemon: Daemon) -> str:
    _, key = daemon.remote_devices.add("hub", kind="machine")
    return key


async def test_a_machine_peer_lists_own_rows_and_hears_no_broadcast(daemon: Daemon) -> None:
    key = _machine_key(daemon)
    async with (
        window(daemon, ["machines"]) as viewer,
        window(daemon) as plain,
        window(daemon, ["machines"], token=key, path="/phone") as hub,
    ):
        assert (await recv(viewer))["type"] == "machine_list"
        own = await ask(hub, {"type": "list_sessions"}, "session_list")
        assert own["type"] == "session_list"
        refused = await ask(hub, {"type": "open_workspace", "path": "/tmp"})
        assert refused["code"] == "not_allowed"

        await daemon._push_job_list()
        assert (await _next(viewer))["type"] == "job_list"
        await daemon._maybe_broadcast_cu_focus("none")
        assert (await _next(viewer))["type"] == "focus_window"
        assert (await _next(plain))["type"] == "focus_window"
        with pytest.raises(TimeoutError):
            await recv(hub, within=0.3)


async def test_job_list_skips_non_viewers_and_focus_window_stays_local(daemon: Daemon) -> None:
    phone_key = daemon.remote_devices.add("Pixel")[1]
    async with (
        window(daemon) as plain,
        window(daemon, ["machines"], token=phone_key, path="/phone") as phone,
    ):
        assert (await recv(phone))["type"] == "machine_list"
        await daemon._push_job_list()
        assert (await recv(phone))["type"] == "job_list"
        await daemon._maybe_broadcast_cu_focus("none")
        with pytest.raises(TimeoutError):
            await recv(phone, within=0.3)
        assert (await _next(plain))["type"] == "focus_window"
        with pytest.raises(TimeoutError):
            await _next(plain, within=0.3)  # no job_list: not a viewer


async def test_a_viewer_without_the_feature_sees_what_it_saw_before(daemon: Daemon) -> None:
    async with window(daemon) as plain:
        rows = await ask(plain, {"type": "list_sessions"}, "session_list")
        assert rows["type"] == "session_list"
        machines = await ask(plain, {"type": "list_machines"}, "machine_list")
        # Loopback may still ask; nothing is pushed unasked.
        assert machines["type"] == "machine_list" and machines["machines"] == []
        with pytest.raises(TimeoutError):
            await recv(plain, within=0.3)


async def test_a_remote_peer_setup_state_has_no_devices_or_address(daemon: Daemon) -> None:
    phone_key = daemon.remote_devices.add("Pixel")[1]
    machine_key = _machine_key(daemon)
    async with window(daemon) as local:
        state = await ask(local, {"type": "get_setup_state"}, "setup_state")
        assert "remote_devices" in state and "remote_bind" in state
    for token in (phone_key, daemon.ws_server.remote_token):
        async with window(daemon, token=token, path="/phone") as remote:
            state = await ask(remote, {"type": "get_setup_state"}, "setup_state")
            assert state["type"] == "setup_state"
            for key in ("remote_devices", "remote_bind", "remote_port"):
                assert key not in state, (token[:0], key)
    async with window(daemon, token=machine_key, path="/phone") as hub:
        assert (await ask(hub, {"type": "get_setup_state"}))["code"] == "not_allowed"


async def test_a_remote_peer_removing_itself_hears_no_devices_or_address(daemon: Daemon) -> None:
    # The self-removal reply leaves by its own path (it must beat the
    # close) and still gets the remote-peer scrub (A.11).
    daemon.remote_devices.add("Other phone")
    for kind, features in (("phone", None), ("machine", ["bridge"])):
        device, key = daemon.remote_devices.add("me", kind=kind)
        async with window(daemon, features, token=key, path="/phone") as me:
            forget = {"type": "remove_remote_device", "device_id": device.id}
            reply = await ask(me, forget, "setup_state")
        assert reply["type"] == "setup_state", reply
        for field in REMOTE_SETUP_STATE_DROPS:
            assert field not in reply, (kind, field)
    assert [d.name for d in daemon.remote_devices.summaries()] == ["Other phone"]
