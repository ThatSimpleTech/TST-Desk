"""The trust card names the setup command trusting runs (STATES1-08)."""

from __future__ import annotations

from pathlib import Path

from tstd.session import Session
from tstd.session_lifecycle import _config_trust
from tstd.workspace_trust import TrustStore


def test_the_card_carries_the_setup_command(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".tst").mkdir(parents=True)
    (ws / ".tst" / "config.yaml").write_text(
        "worktree:\n  setup: npm ci && ./scripts/bootstrap.sh\n", encoding="utf-8"
    )
    store = TrustStore(tmp_path / "data")
    session = Session(str(ws))
    session.trust_store = store
    session.config_view = store.view(ws)
    trust = _config_trust(session)
    assert trust is not None
    assert trust.setup_command == "npm ci && ./scripts/bootstrap.sh"


def test_no_setup_command_is_none(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".tst").mkdir(parents=True)
    (ws / ".tst" / "config.yaml").write_text(
        "boundary:\n  network: [example.com]\n", encoding="utf-8"
    )
    store = TrustStore(tmp_path / "data")
    session = Session(str(ws))
    session.trust_store = store
    session.config_view = store.view(ws)
    trust = _config_trust(session)
    assert trust is not None and trust.setup_command is None
