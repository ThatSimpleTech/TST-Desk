"""Suite-wide fixtures.

The application ping (TD-1716) is a liveness frame, not a reply. A loaded
suite can stall a settings-wire test past 15s, and ``await ws.recv()`` then
returns ``ping`` instead of the ack. Production clients skip it. Tests that
are not about the cadence should not see it either.
"""

from __future__ import annotations

import pytest

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
