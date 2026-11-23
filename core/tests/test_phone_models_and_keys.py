"""A paired phone adds models and keys only if the computer said so (TD-5296).

A new model entry names a host the conversation is sent to, and a key or a
key's host decides where that key goes. From a phone that was a standing
change with nobody at the computer to see it. It is now the phone
permission ``models_and_keys``. Choosing among the models already there
(the picker, TD-5242) stays open to a phone.
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
from tstd.phone_verbs import PHONE_OPEN, required_capability

GRANT = {"type": "set_phone_permission", "capability": "models_and_keys", "enabled": True}
GRANTED = frozenset({"models_and_keys"})
CHANGES: list[tuple[str, dict[str, Any]]] = [
    (
        "add_model",
        {"label": "Elsewhere", "base_url": "https://models.example.com/v1", "slug": "m"},
    ),
    ("set_api_key", {"api_key": "sk-test", "credential": "other"}),
    ("set_credential", {"name": "Other", "base_url": "https://keys.example.com/v1"}),
    ("set_tier_credential", {"preset": "local", "tier": "brain", "credential": "other"}),
]


@pytest.mark.parametrize(("kind", "fields"), CHANGES)
def test_each_needs_the_permission(kind: str, fields: dict[str, Any]) -> None:
    msg = _msg(kind, **fields)
    assert required_capability(msg) == "models_and_keys"
    assert kind not in PHONE_OPEN
    for phone in (PHONE_VIEWER, PHONE_PLAIN):
        assert _code(check(msg, phone, bridged=False)) == "phone_permission_off"
        assert _code(
            check(msg, phone, bridged=False, grants={"mcp_servers", "approval_switches"})
        ) == ("phone_permission_off")
        assert check(msg, phone, bridged=False, grants=GRANTED) is None
    assert _code(check(msg, PLAIN_REMOTE, bridged=False, grants=GRANTED)) == "phone_permission_off"
    assert _code(check(msg, MACHINE, bridged=False, grants=GRANTED)) == "not_allowed"
    for own in (LOOPBACK_PLAIN, VIEWER):
        assert check(msg, own, bridged=False) is None


@pytest.mark.parametrize(
    "kind", ["switch_preset", "duplicate_preset", "add_grok_preset", "validate_api_key"]
)
def test_the_picker_and_what_adds_no_host_stay_open(kind: str) -> None:
    assert kind in PHONE_OPEN


async def test_a_phone_cannot_point_a_key_at_a_host_by_default(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    change = {"type": "set_credential", "name": "Other", "base_url": "https://keys.example.com/v1"}
    try:
        reply = await _fresh(phone, change)
        assert reply["type"] == "error" and reply["code"] == "phone_permission_off"
        assert "Adding models and keys" in reply["message"]
        assert "other" not in daemon.config.credentials
        await _fresh(ws, GRANT)
        await _drain(ws)
        done = await _fresh(phone, change)
        assert done["type"] == "setup_state", done
        assert daemon.config.credentials["other"].base_url == "https://keys.example.com/v1"
        told = [e for e in await _drain(ws) if e["type"] == "phone_change"]
        assert [(e["capability"], e["summary"]) for e in told] == [
            ("models_and_keys", "changed the API key 'Other'.")
        ]
    finally:
        await phone.close()
        await _stop(task, ws)
