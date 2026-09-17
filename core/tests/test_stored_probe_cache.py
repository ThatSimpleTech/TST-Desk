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
