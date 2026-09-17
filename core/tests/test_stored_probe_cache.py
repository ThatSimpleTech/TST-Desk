"""Keychain presence-probe cache (TD-4835).

A keychain read can prompt when an item's ACL predates this build's
signature, so probing on every setup_state storms the user with dialogs.
Presence is cached and invalidated by the key-mutation handlers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tstd.daemon import Daemon
from tstd.keychain import KeychainError


class TestStoredProbeCache:
    async def test_presence_is_probed_once_then_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = 0

        async def probe(_credential: str) -> str:
            nonlocal calls
            calls += 1
            return "sk-x"

        monkeypatch.setattr("tstd.daemon.get_api_key", probe)
        daemon = Daemon(data_dir=tmp_path / "data")
        assert await daemon._credential_is_stored("typesafe") is True
        assert await daemon._credential_is_stored("typesafe") is True
        assert calls == 1

    async def test_mutation_invalidates_the_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = 0

        async def probe(_credential: str) -> str:
            nonlocal calls
            calls += 1
            raise KeychainError("not found")

        monkeypatch.setattr("tstd.daemon.get_api_key", probe)
        daemon = Daemon(data_dir=tmp_path / "data")
        assert await daemon._credential_is_stored("typesafe") is False
        daemon._invalidate_stored_probe()
        assert await daemon._credential_is_stored("typesafe") is False
        assert calls == 2


class TestSetJudgmentsCredential:
    """The set_judgments handler rebinds the connector's credential."""

    def _daemon(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Daemon:
        from tests.test_dispatch import make_config
        from tstd.protocol import SetupState

        daemon = Daemon(data_dir=tmp_path / "data")
        daemon.config = make_config()
        monkeypatch.setattr("tstd.daemon.save_judgments", lambda cfg: None)

        async def fake_state() -> SetupState:
            return SetupState(has_api_key=True, active_preset="demo")

        monkeypatch.setattr(daemon, "_setup_state_event", fake_state)
        return daemon

    async def test_rebinds_to_a_known_credential(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = self._daemon(tmp_path, monkeypatch)
        reply = await daemon._handle_message(
            '{"type": "set_judgments", "verification": false, "semantic_breaker": false,'
            ' "candidate_selection": false, "backend": "typesafe",'
            ' "typesafe_credential": "openrouter"}',
            None,
        )
        assert reply is not None
        assert daemon.config.judgments.typesafe_credential == "openrouter"
        assert daemon.config.judgments.backend == "typesafe"

    async def test_rejects_an_unknown_credential(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = self._daemon(tmp_path, monkeypatch)
        reply = await daemon._handle_message(
            '{"type": "set_judgments", "verification": false, "semantic_breaker": false,'
            ' "candidate_selection": false, "typesafe_credential": "nope"}',
            None,
        )
        assert reply is not None and "Unknown credential" in reply
        assert daemon.config.judgments.typesafe_credential == "typesafe"
