"""A paired phone changes computer use only if the computer said so (TD-5297).

Computer use moves this computer's pointer and types on its keyboard. A
phone could turn it on, switch it to full control or hide the indicators
that show it is running, with nobody at the computer. That is now the
phone permission ``computer_use``. What only tightens stays open: turning
it off, turning every indicator on, engaging the stop. Releasing the stop
is never a phone's, granted or not (owner decision).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_add_model import _stop
from tests.test_mcp_peer import _drain, _fresh, _pair
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
from tstd.peer_policy import check
from tstd.phone_verbs import required_capability

GRANTED = frozenset({"computer_use"})
POLICY = {"mode": "background", "unhide_on_finish": True, "denied_apps": []}
ALL_SHOWN = {"glow": True, "agent_cursor": True, "show_on_real_display": True}

LOOSENS: list[tuple[str, dict[str, Any]]] = [
    ("set_cu_policy", {**POLICY, "enabled": True}),
    ("set_cu_policy", {**POLICY, "enabled": True, "mode": "full_control"}),
    ("set_cu_indicators", {**ALL_SHOWN, "glow": False}),
    ("set_cu_indicators", {**ALL_SHOWN, "show_on_real_display": False}),
]
TIGHTENS: list[tuple[str, dict[str, Any]]] = [
    ("set_cu_policy", {**POLICY, "enabled": False}),
    ("set_cu_indicators", ALL_SHOWN),
    ("set_cu_kill", {"killed": True}),
]


@pytest.mark.parametrize(("kind", "fields"), LOOSENS)
def test_turning_it_on_or_hiding_it_needs_the_permission(kind: str, fields: dict[str, Any]) -> None:
    msg = _msg(kind, **fields)
    assert required_capability(msg) == "computer_use"
    for phone in (PHONE_VIEWER, PHONE_PLAIN):
        assert _code(check(msg, phone, bridged=False)) == "phone_permission_off"
        assert _code(check(msg, phone, bridged=False, grants={"models_and_keys"})) == (
            "phone_permission_off"
        )
        assert check(msg, phone, bridged=False, grants=GRANTED) is None
    assert _code(check(msg, PLAIN_REMOTE, bridged=False, grants=GRANTED)) == "phone_permission_off"
    assert _code(check(msg, MACHINE, bridged=False, grants=GRANTED)) == "not_allowed"
    for own in (LOOPBACK_PLAIN, VIEWER):
        assert check(msg, own, bridged=False) is None


@pytest.mark.parametrize(("kind", "fields"), TIGHTENS)
def test_what_only_tightens_needs_nothing(kind: str, fields: dict[str, Any]) -> None:
    msg = _msg(kind, **fields)
    assert required_capability(msg) is None
    for peer in (PHONE_VIEWER, PHONE_PLAIN, LOOPBACK_PLAIN):
        assert check(msg, peer, bridged=False) is None, peer


def test_releasing_the_stop_is_never_a_phones() -> None:
    release = _msg("set_cu_kill", killed=False)
    for phone in (PHONE_VIEWER, PHONE_PLAIN, PLAIN_REMOTE):
        assert _code(check(release, phone, bridged=False, grants=GRANTED)) == "loopback_only"
    assert check(release, LOOPBACK_PLAIN, bridged=False) is None


async def test_a_phone_turns_it_on_only_after_the_grant_and_the_computer_is_told(
    tmp_path: Path,
) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    on = {"type": "set_cu_policy", **POLICY, "enabled": True}
    try:
        was = daemon.cu_policy.enabled
        refused = await _fresh(phone, on)
        assert refused["type"] == "error" and refused["code"] == "phone_permission_off"
        assert "Computer use settings" in refused["message"]
        assert daemon.cu_policy.enabled == was
        grant = {"type": "set_phone_permission", "capability": "computer_use", "enabled": True}
        await _fresh(ws, grant)
        await _drain(ws)
        assert (await _fresh(phone, on))["type"] == "setup_state"
        assert daemon.cu_policy.enabled is True
        told = [e for e in await _drain(ws) if e["type"] == "phone_change"]
        assert [(e["capability"], e["summary"]) for e in told] == [
            ("computer_use", "turned computer use on (background).")
        ]
    finally:
        await phone.close()
        await _stop(task, ws)
