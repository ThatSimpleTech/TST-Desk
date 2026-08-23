"""Named API keys on the wire (TD-1717)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tstd.config import ModelConfig, default_config_yaml, shipped_credential_base_url
from tstd.daemon import Daemon
from tstd.keychain import KeychainError
from tstd.protocol import parse_client_message

_TAG = "sk-named-test-key"  # tst-secret-ok


class FakeKeychain:
    def __init__(self) -> None:
        self.stored: dict[str, str] = {}

    async def get(self, provider_name: str = "openrouter") -> str:
        if provider_name not in self.stored:
            raise KeychainError(f"API key not found in keychain for {provider_name!r}.")
        return self.stored[provider_name]

    async def store(self, api_key: str, provider_name: str = "openrouter") -> None:
        self.stored[provider_name] = api_key

    async def delete(self, provider_name: str = "openrouter") -> None:
        if provider_name not in self.stored:
            raise KeychainError(f"API key not found in keychain for {provider_name!r}.")
        del self.stored[provider_name]


@pytest.fixture
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
    fk = FakeKeychain()

    async def _get(provider_name: str = "openrouter") -> str:
        return await fk.get(provider_name)

    async def _store(api_key: str, provider_name: str = "openrouter") -> None:
        await fk.store(api_key, provider_name)

    async def _delete(provider_name: str = "openrouter") -> None:
        await fk.delete(provider_name)

    monkeypatch.setattr("tstd.daemon.get_api_key", _get)
    monkeypatch.setattr("tstd.daemon.store_api_key", _store)
    monkeypatch.setattr("tstd.daemon.delete_api_key", _delete)
    monkeypatch.setattr("tstd.keychain.get_api_key", _get)
    return fk


def _config() -> ModelConfig:
    return ModelConfig.model_validate(yaml.safe_load(default_config_yaml()))


def _daemon(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> Daemon:
    monkeypatch.setattr("tstd.daemon.cached_config", lambda: _config())
    return Daemon(data_dir=data_dir)


async def _send(daemon: Daemon, payload: dict[str, Any]) -> dict[str, Any]:
    raw = await daemon._handle_message(json.dumps(payload), None)
    assert raw is not None
    return dict(json.loads(raw))


class TestCatalog:
    async def test_setup_state_lists_openrouter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keychain: FakeKeychain
    ) -> None:
        daemon = _daemon(monkeypatch, tmp_path)
        reply = await daemon._setup_state_event()
        ids = [c.id for c in reply.credentials]
        assert "openrouter" in ids
        assert all(c.name and not c.id.startswith("sk-") for c in reply.credentials)
        assert reply.tier_credentials["brain"] == "openrouter"
        assert reply.tier_loopback["brain"] is False

    async def test_set_api_key_with_a_name_creates_a_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keychain: FakeKeychain
    ) -> None:
        daemon = _daemon(monkeypatch, tmp_path)
        monkeypatch.setattr("tstd.daemon.load_config", lambda: _config())
        writes: list[tuple[str, str]] = []

        def _save(
            credential_id: str,
            name: str,
            path: Path | None = None,
            **_kwargs: object,
        ) -> Path:
            writes.append((credential_id, name))
            return Path("/unused")

        monkeypatch.setattr("tstd.daemon.save_credential", _save)
        reply = await _send(
            daemon,
            {"type": "set_api_key", "api_key": _TAG, "name": "Local"},
        )
        assert fake_keychain.stored["local"] == _TAG
        assert ("local", "Local") in writes
        assert _TAG not in json.dumps(reply)

    async def test_bound_loopback_reads_that_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keychain: FakeKeychain
    ) -> None:
        fake_keychain.stored["local"] = _TAG
        cfg = _config()
        cfg.presets["local"].brain = cfg.presets["local"].brain.model_copy(
            update={"credential": "local"}
        )
        cfg = cfg.model_copy(update={"active_preset": "local"})
        monkeypatch.setattr("tstd.daemon.cached_config", lambda: cfg)
        daemon = Daemon(data_dir=tmp_path)
        client = await daemon._build_client(cfg.tier("brain"))
        try:
            assert client.api_key == _TAG
        finally:
            await client.close()

    async def test_unbound_loopback_still_skips_the_keychain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keychain: FakeKeychain
    ) -> None:
        daemon = _daemon(monkeypatch, tmp_path)
        daemon.config = daemon.config.model_copy(update={"active_preset": "local"})
        client = await daemon._build_client(daemon.config.tier("brain"))
        try:
            assert client.api_key is None
        finally:
            await client.close()
        assert fake_keychain.stored == {}

    async def test_bound_key_calls_its_own_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keychain: FakeKeychain
    ) -> None:
        fake_keychain.stored["openrouter"] = _TAG
        cfg = _config()
        cfg.presets["local"].worker = cfg.presets["local"].worker.model_copy(
            update={"credential": "openrouter", "slug": "demo/worker"}
        )
        cfg = cfg.model_copy(update={"active_preset": "local"})
        monkeypatch.setattr("tstd.daemon.cached_config", lambda: cfg)
        daemon = Daemon(data_dir=tmp_path)
        client = await daemon._build_client(cfg.tier("worker"))
        try:
            assert client.api_key == _TAG
            assert client.base_url == shipped_credential_base_url()
            assert "11434" not in client.base_url
        finally:
            await client.close()

    async def test_setup_state_includes_the_key_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keychain: FakeKeychain
    ) -> None:
        daemon = _daemon(monkeypatch, tmp_path)
        reply = await daemon._setup_state_event()
        openrouter = next(c for c in reply.credentials if c.id == "openrouter")
        assert openrouter.base_url == shipped_credential_base_url()
        assert _TAG not in openrouter.base_url


class TestParse:
    def test_new_messages_parse(self) -> None:
        cred = parse_client_message('{"type": "set_credential", "name": "Local"}')
        assert cred.type == "set_credential"
        gone = parse_client_message('{"type": "delete_credential", "credential": "local"}')
        assert gone.type == "delete_credential"
        bind = parse_client_message(
            '{"type": "set_tier_credential", "preset": "local", "tier": "brain", "credential": ""}'
        )
        assert bind.type == "set_tier_credential"
        assert bind.credential == ""
        with_url = parse_client_message(
            '{"type": "set_credential", "name": "OpenRouter",'
            ' "credential": "openrouter", "base_url": "http://127.0.0.1:9/v1"}'
        )
        assert with_url.type == "set_credential"
        assert with_url.base_url == "http://127.0.0.1:9/v1"
