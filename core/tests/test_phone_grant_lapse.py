"""A timed phone permission runs out on the daemon's clock (owner decision #6, TD-5302).

"Turn on for 1 hour" was already in Settings (TD-5279), and the policy
reads the clock on every message. What the computer did not get was a word
when it happened: the Settings row just read "Off" again. Now the daemon
tells the computer's windows, once per grant, and never a phone.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from tests.test_add_model import _stop
from tests.test_mcp_peer import _drain, _fresh, _pair
from tests.test_models_on_phone import _seeded

RENAME = {"type": "set_credential", "name": "Router", "credential": "openrouter"}


async def _until(ws: Any, kind: str) -> dict[str, Any]:
    async with asyncio.timeout(10):
        while True:
            frame = dict(json.loads(await ws.recv()))
            if frame["type"] == kind:
                return frame


async def test_a_timed_grant_stops_and_the_computer_is_told(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    # The expiry is stored to the whole second, so a grant of n seconds
    # lasts between n-1 and n: three leaves room for the first message.
    grant = {
        "type": "set_phone_permission",
        "capability": "models_and_keys",
        "enabled": True,
        "expires_in_seconds": 3,
    }
    try:
        state = await _fresh(ws, grant)
        assert state["phone_permissions"]["models_and_keys"]["enabled"] is True
        assert (await _fresh(phone, RENAME))["type"] == "setup_state"

        lapsed = await _until(ws, "phone_permission_lapsed")
        assert lapsed["capability"] == "models_and_keys"
        assert lapsed["label"] == "Adding models and keys"
        assert lapsed["expired_at"] == state["phone_permissions"]["models_and_keys"]["expires_at"]

        refused = await _fresh(phone, {**RENAME, "name": "Again"}, want="error")
        assert refused["code"] == "phone_permission_off"
        assert daemon.config.credentials["openrouter"].name == "Router"
        # The phone is not sent the lapse; only the computer's windows are.
        assert all(f["type"] != "phone_permission_lapsed" for f in await _drain(phone))
    finally:
        await phone.close()
        await _stop(task, ws)


async def test_switching_off_by_hand_is_not_a_lapse(tmp_path: Path) -> None:
    _daemon, task, ws = await _seeded(tmp_path)
    base = {"type": "set_phone_permission", "capability": "models_and_keys"}
    try:
        await _fresh(ws, {**base, "enabled": True, "expires_in_seconds": 1})
        await _fresh(ws, {**base, "enabled": False})
        await asyncio.sleep(1.8)
        assert all(f["type"] != "phone_permission_lapsed" for f in await _drain(ws))
    finally:
        await _stop(task, ws)
