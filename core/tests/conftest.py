"""Suite-wide fixtures.

The application ping (TD-1716) is a liveness frame, not a reply. A loaded
suite can stall a settings-wire test past 15s, and ``await ws.recv()`` then
returns ``ping`` instead of the ack. Production clients skip it. Tests that
are not about the cadence should not see it either.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from tstd.keychain import KeychainError

_KEEP_REAL_PING_INTERVAL = {
    "test_default_interval_leaves_room_for_a_missed_frame",
}


@pytest.fixture(autouse=True)
def _quiet_application_pings(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop the 15s ping unless the test is pinning the real cadence."""
    if request.node.name in _KEEP_REAL_PING_INTERVAL:
        return
    monkeypatch.setattr("tstd.ws.PING_INTERVAL_SECONDS", 0.0)


# Seams that would otherwise call the developer's live keychain. setup_state
# probes every named credential via get_api_key; a locked Secret Service
# waits on a prompt and the suite hangs (TD-1105). test_keychain opts out
# because it is the module that owns the real backends.
_KEYCHAIN_SEAMS = (
    "tstd.keychain.get_api_key",
    "tstd.keychain.store_api_key",
    "tstd.keychain.delete_api_key",
    "tstd.keychain.get_slack_webhook_url",
    "tstd.keychain.store_slack_webhook_url",
    "tstd.keychain.delete_slack_webhook_url",
    "tstd.keychain.get_ntfy_topic_url",
    "tstd.keychain.store_ntfy_topic_url",
    "tstd.keychain.delete_ntfy_topic_url",
    "tstd.daemon.get_api_key",
    "tstd.notify.slack.get_slack_webhook_url",
    "tstd.notify.ntfy.get_ntfy_topic_url",
)


async def _isolated_keychain(*_args: object, **_kwargs: object) -> Any:
    raise KeychainError("keychain isolated in tests")


@pytest.fixture(autouse=True)
def _isolate_live_keychain(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite off the host Secret Service / Keychain / CredMan."""
    if request.module.__name__.endswith("test_keychain"):
        return
    isolated: Callable[..., Awaitable[Any]] = _isolated_keychain
    for seam in _KEYCHAIN_SEAMS:
        monkeypatch.setattr(seam, isolated)
