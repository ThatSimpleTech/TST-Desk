"""TST Desk's own writes into an untrusted or half-valid config (TD-5113).

Two holes the review found in the trust gate:

- A repo can hide loosening behind a section the loader refuses (a bad
  ``caps`` value hides the ``boundary.network`` beside it, because the
  whole boundary falls back to the defaults). A budget saved over the bad
  value made the section valid, and the write used to stamp the new hash
  trusted, so the allowlist applied with no card. A write now keeps trust
  only for what was in force before it, plus the key it sets.
- In a pending workspace the card still offered Always allow, and the
  daemon refused it without resolving the call, so the turn hung with no
  card. The card now offers no Always allow there, and a stray one
  approves the call once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.machine_helpers import eventually
from tests.test_artifacts import _open, _shutdown
from tests.test_two_daemons import _Steps
from tests.test_workspace_trust import LOOSE, SHELL_AUTO, _send, _walls, _write
from tstd.boundary_config import save_spend_cap
from tstd.daemon import Daemon
from tstd.mock import Script
from tstd.protocol import ApprovalRequest, ToolResult
from tstd.workspace_trust import TrustStore, read_view

# The network allowlist hides behind an invalid cap: the loader refuses
# the whole boundary, so the card has nothing to list.
HIDDEN = """\
boundary:
  network:
    - evil.example
caps:
  spend_usd: -1
"""


def _keys(view: Any) -> list[str]:
    return [item.key for item in view.loosened]


def test_a_write_that_exposes_hidden_loosening_leaves_the_file_pending(tmp_path: Path) -> None:
    store = TrustStore(tmp_path / "data")
    ws = tmp_path / "ws"
    _write(ws, HIDDEN)
    assert read_view(ws, store).state == "none"  # nothing the card could show

    after = store.write_through(ws, lambda: save_spend_cap(ws, 10), owns=("caps.spend_usd",))

    assert after.state == "pending"
    assert _keys(after) == ["boundary.network"]
    assert read_view(ws, store).state == "pending"
    assert read_view(ws, store).effective is None


def test_trust_does_not_carry_to_a_setting_the_card_never_showed(tmp_path: Path) -> None:
    store = TrustStore(tmp_path / "data")
    ws = tmp_path / "ws"
    sha = _write(ws, SHELL_AUTO + HIDDEN)
    shown = read_view(ws, store)
    assert _keys(shown) == ["policy.rules"]
    store.record(ws, sha, "trusted")  # the user trusted what the card listed

    after = store.write_through(ws, lambda: save_spend_cap(ws, 10), owns=("caps.spend_usd",))

    assert after.state == "pending"
    assert _keys(after) == ["policy.rules", "boundary.network"]


def test_a_write_keeps_trust_for_what_was_in_force_and_its_own_key(tmp_path: Path) -> None:
    store = TrustStore(tmp_path / "data")
    ws = tmp_path / "ws"
    store.record(ws, _write(ws, LOOSE), "trusted")

    after = store.write_through(ws, lambda: save_spend_cap(ws, 900), owns=("caps.spend_usd",))

    assert after.state == "trusted"
    assert read_view(ws, store).state == "trusted"
    assert "caps.spend_usd: 900" in [f"{i.key}: {i.value}" for i in after.loosened]


def test_a_write_cannot_bring_in_a_key_it_does_not_own(tmp_path: Path) -> None:
    store = TrustStore(tmp_path / "data")
    ws = tmp_path / "ws"
    store.record(ws, _write(ws, LOOSE), "trusted")

    after = store.write_through(
        ws, lambda: _write(ws, LOOSE + "hooks: [run.sh]\n"), owns=("caps.spend_usd",)
    )

    assert after.state == "pending"


# ── through the daemon ──────────────────────────────────────────────────


async def test_a_budget_that_exposes_a_hidden_allowlist_shows_the_card(tmp_path: Path) -> None:
    """The review's repro: ``set_spend_cap`` over a bad cap must not trust the network list."""
    ws = tmp_path / "ws"
    _write(ws, HIDDEN)
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, ws)
    try:
        opened = _walls(daemon, sid)[-1]
        assert opened.network == "deny" and opened.config_trust is None

        reply = await _send(daemon, {"type": "set_spend_cap", "session_id": sid, "spend_usd": 10})

        assert reply is None
        wall = _walls(daemon, sid)[-1]
        assert wall.network == "deny"
        assert wall.config_trust is not None and wall.config_trust.state == "pending"
        assert [s.key for s in wall.config_trust.loosened] == ["boundary.network"]
        assert wall.source == "defaults — workspace config not trusted yet"
        session = daemon.session_registry.get(sid)
        assert session is not None
        assert session.boundary_config.allowed_hosts == frozenset()
    finally:
        await _shutdown(daemon, sid)


async def test_always_allow_is_not_offered_while_untrusted_and_a_stray_one_approves_once(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    _write(ws, SHELL_AUTO)
    config = (ws / ".tst" / "config.yaml").read_bytes()
    steps = [
        Script(kind="tool_call", tool_name="shell", tool_arguments='{"command": "touch a.txt"}'),
        Script(kind="stream", content="Done."),
    ]
    daemon = Daemon(data_dir=tmp_path / "data", provider=_Steps(steps))
    sid = await _open(daemon, ws)
    try:
        session = daemon.session_registry.get(sid)
        assert session is not None
        log = session.event_log

        def of(kind: type[Any]) -> list[Any]:
            return [e for e in log.all_events if isinstance(e, kind)]

        await _send(daemon, {"type": "user_message", "session_id": sid, "content": "go"})
        await eventually(lambda: len(of(ApprovalRequest)) == 1, within=20)
        parked = of(ApprovalRequest)[0]
        assert parked.decision_class == "B"
        assert parked.proposed_always_allow is None  # the card offers approve and deny only

        # A client that sends it anyway (a card from before the file changed).
        reply = await _send(
            daemon,
            {"type": "always_allow", "session_id": sid, "tool_call_id": parked.tool_call_id},
        )

        assert reply is not None and reply["code"] == "workspace_config_untrusted"
        assert reply["message"].startswith("Approved once.")
        await eventually(lambda: len(of(ToolResult)) == 1, within=20)
        assert of(ToolResult)[0].status == "success"
        assert (ws / "a.txt").exists()
        assert (ws / ".tst" / "config.yaml").read_bytes() == config
        assert session.get_pending_approval(parked.tool_call_id) is None
    finally:
        await _shutdown(daemon, sid)
