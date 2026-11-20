"""A paired phone turns approvals off only if the computer said so (TD-5283).

Skip-all persists machine-wide and releases every parked call, Class C
included. A phone could switch it on with nothing shown at the computer.
It is now the phone permission ``approval_switches``. Turning approvals
back on needs nothing: that only tightens.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_add_model import _stop
from tests.test_mcp_peer import _drain, _fresh, _pair
from tests.test_models_on_phone import _phone as _legacy_token_client
from tests.test_models_on_phone import _seeded
from tests.test_peer_policy import (
    LOOPBACK_PLAIN,
    MACHINE,
    PHONE_PLAIN,
    PHONE_VIEWER,
    PLAIN_REMOTE,
    VIEWER,
    _code,
    _msg,
)
from tstd.peer_policy import check, required_capability
from tstd.policy import load_skip_all

ON = {"type": "set_skip_all_approvals", "enabled": True}
OFF = {"type": "set_skip_all_approvals", "enabled": False}
GRANT = {"type": "set_phone_permission", "capability": "approval_switches", "enabled": True}
GRANTED = frozenset({"approval_switches"})


def test_turning_approvals_off_needs_the_permission() -> None:
    on = _msg("set_skip_all_approvals", enabled=True)
    assert required_capability(on) == "approval_switches"
    for phone in (PHONE_VIEWER, PHONE_PLAIN):
        assert _code(check(on, phone, bridged=False)) == "phone_permission_off"
        assert _code(check(on, phone, bridged=False, grants={"mcp_servers"})) == (
            "phone_permission_off"
        )
        assert check(on, phone, bridged=False, grants=GRANTED) is None
    # Never the older token, never another computer, always the computer itself.
    assert _code(check(on, PLAIN_REMOTE, bridged=False, grants=GRANTED)) == "phone_permission_off"
    assert _code(check(on, MACHINE, bridged=False, grants=GRANTED)) == "not_allowed"
    for own in (LOOPBACK_PLAIN, VIEWER):
        assert check(on, own, bridged=False) is None


def test_turning_them_back_on_needs_nothing() -> None:
    off = _msg("set_skip_all_approvals", enabled=False)
    assert required_capability(off) is None
    for peer in (PHONE_VIEWER, PHONE_PLAIN, PLAIN_REMOTE, LOOPBACK_PLAIN):
        assert check(off, peer, bridged=False) is None, peer


async def test_a_phone_cannot_turn_approvals_off_by_default(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    token_client = await _legacy_token_client(daemon)
    try:
        for peer in (phone, token_client):
            reply = await _fresh(peer, ON)
            assert reply["type"] == "error" and reply["code"] == "phone_permission_off"
            assert "Turning approvals off" in reply["message"]
        assert daemon.skip_all_approvals is False
        assert load_skip_all(daemon.data_dir) is False
        # The MCP permission does not cover it.
        await _fresh(ws, {**GRANT, "capability": "mcp_servers"})
        assert (await _fresh(phone, ON))["code"] == "phone_permission_off"
        assert daemon.skip_all_approvals is False
    finally:
        await token_client.close()
        await phone.close()
        await _stop(task, ws)


async def test_after_the_computer_grants_a_phone_can_and_the_computer_is_told(
    tmp_path: Path,
) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    try:
        granted = await _fresh(ws, GRANT)
        assert granted["phone_permissions"]["approval_switches"]["enabled"] is True
        assert granted["phone_permissions"]["mcp_servers"]["enabled"] is False
        reply = await _fresh(phone, ON)
        assert reply["type"] == "setup_state" and reply["skip_all_approvals"] is True
        assert load_skip_all(daemon.data_dir) is True
        heard = await _drain(ws)
        assert [e for e in heard if e["type"] == "phone_change"] == [
            {
                "type": "phone_change",
                "seq": 1,
                "device_name": "Pixel 8",
                "capability": "approval_switches",
                "summary": "turned approval prompts off for every session on this computer.",
            }
        ]
        states = [e for e in heard if e["type"] == "setup_state"]
        assert states and states[-1]["skip_all_approvals"] is True
    finally:
        await phone.close()
        await _stop(task, ws)


async def test_a_phone_can_always_turn_approvals_back_on(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    try:
        assert (await _fresh(ws, ON))["skip_all_approvals"] is True
        await _drain(ws)
        reply = await _fresh(phone, OFF)
        assert reply["type"] == "setup_state" and reply["skip_all_approvals"] is False
        assert load_skip_all(daemon.data_dir) is False
        # Tightening is no use of a permission: nobody is told.
        assert [e for e in await _drain(ws) if e["type"] == "phone_change"] == []
    finally:
        await phone.close()
        await _stop(task, ws)


async def test_the_computer_turning_them_off_is_no_phone_change(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    try:
        assert (await _fresh(ws, ON))["skip_all_approvals"] is True
        assert [e for e in await _drain(ws) if e["type"] == "phone_change"] == []
        assert daemon.skip_all_approvals is True
    finally:
        await _stop(task, ws)
