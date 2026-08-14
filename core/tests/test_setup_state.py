"""Tests for the onboarding wire (TD-1101 first-run wizard).

``get_setup_state`` answers the wizard's first-run question —
``has_api_key`` comes from the keychain, never from disk config, so the
signal survives daemon restarts.  ``set_api_key`` stores via the OS keychain
(and never echoes the key back — not to the response, not to logs).
``validate_api_key`` proves a stored key with one cheap one-token call and
maps failures to actionable detail.  ``set_preset`` persists the choice to
the user config and applies to new sessions immediately.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tstd.config import cached_config
from tstd.daemon import Daemon
from tstd.keychain import KeychainError
from tstd.protocol import PROTOCOL_VERSION
from tstd.provider import ProviderError


async def _connect_and_handshake(uri: str, token: str) -> Any:
    """Open a connection and perform the hello handshake."""
    ws = await connect(uri)
    await ws.send(
        json.dumps(
            {
                "type": "hello",
                "token": token,
                "version": PROTOCOL_VERSION,
            }
        )
    )
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _start_daemon(tmp: Path) -> tuple[Daemon, asyncio.Task[Any]]:
    """Start a daemon on a temp dir and wait for its port."""
    daemon = Daemon(data_dir=tmp)
    task = asyncio.create_task(daemon.run())
    for _ in range(50):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


async def _ask(ws: Any, msg: dict[str, Any]) -> dict[str, Any]:
    """Send one client message, return the first response event."""
    await ws.send(json.dumps(msg))
    return dict(json.loads(await ws.recv()))


class FakeKeychain:
    """In-memory stand-in for get/store API-key helpers."""

    def __init__(self) -> None:
        self.stored: dict[str, str] = {}

    async def get(self, provider_name: str = "openrouter") -> str:
        if provider_name not in self.stored:
            raise KeychainError(f"API key not found in keychain for {provider_name!r}.")
        return self.stored[provider_name]

    async def store(self, api_key: str, provider_name: str = "openrouter") -> None:
        self.stored[provider_name] = api_key

    def fail_store(self, e: Exception) -> None:
        async def _fail(api_key: str, provider_name: str = "openrouter") -> None:
            raise e

        self.store = _fail  # type: ignore[method-assign]


class FakeProviderClient:
    """Class-level stand-in: from_keychain yields instances whose
    chat_completion returns a scripted result."""

    scripted: Any = object()  # non-ProviderError ⇒ success
    raise_on_build: Exception | None = None

    @classmethod
    async def from_keychain(cls, base_url: str) -> FakeProviderClient:
        if cls.raise_on_build is not None:
            raise cls.raise_on_build
        return cls()

    async def chat_completion(self, request: Any) -> Any:
        return self.scripted

    @classmethod
    def reset(cls) -> None:
        cls.scripted = object()
        cls.raise_on_build = None


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point HOME at a temp dir so the suite never reads (or writes) the
    developer's real user config — active_preset must come from the shipped
    default, or the assertions depend on host state."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cached_config.cache_clear()


@pytest.fixture
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
    fk = FakeKeychain()

    # Delegate through wrappers so `fail_store` rebinding is seen by the
    # daemon (patching the bound method would freeze the original).
    async def _get(provider_name: str = "openrouter") -> str:
        return await fk.get(provider_name)

    async def _store(api_key: str, provider_name: str = "openrouter") -> None:
        await fk.store(api_key, provider_name)

    monkeypatch.setattr("tstd.daemon.get_api_key", _get)
    monkeypatch.setattr("tstd.daemon.store_api_key", _store)
    monkeypatch.setattr("tstd.daemon.ProviderClient", FakeProviderClient)
    FakeProviderClient.reset()
    cached_config.cache_clear()
    return fk


class TestSetupState:
    @pytest.mark.asyncio
    async def test_no_key_means_first_run(self, fake_keychain: FakeKeychain) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "get_setup_state"})
                assert resp["type"] == "setup_state"
                assert resp["has_api_key"] is False
                assert resp["active_preset"] == "tst-default"
                assert {"tst-default", "budget", "local"} <= set(resp["presets"])
                await ws.close()
            finally:
                task.cancel()

    @pytest.mark.asyncio
    async def test_key_present_flips_has_api_key(self, fake_keychain: FakeKeychain) -> None:
        fake_keychain.stored["openrouter"] = "sk-test"
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "get_setup_state"})
                assert resp["has_api_key"] is True
                await ws.close()
            finally:
                task.cancel()


class TestSetApiKey:
    @pytest.mark.asyncio
    async def test_store_then_ack(self, fake_keychain: FakeKeychain) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "set_api_key", "api_key": "sk-live-check-12345"})
                assert fake_keychain.stored["openrouter"] == "sk-live-check-12345"
                assert resp["type"] == "setup_state"
                assert resp["has_api_key"] is True
                # The key is never echoed anywhere on the wire.
                assert "sk-live-check-12345" not in json.dumps(resp)
                await ws.close()
            finally:
                task.cancel()

    @pytest.mark.asyncio
    async def test_store_failure_is_a_typed_error(self, fake_keychain: FakeKeychain) -> None:
        fake_keychain.fail_store(KeychainError("keychain locked"))
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "set_api_key", "api_key": "sk-x"})
                assert resp["type"] == "error"
                assert resp["code"] == "key_store_failed"
                await ws.close()
            finally:
                task.cancel()


class TestValidateApiKey:
    @pytest.mark.asyncio
    async def test_success(self, fake_keychain: FakeKeychain) -> None:
        FakeProviderClient.scripted = object()
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "validate_api_key"})
                assert resp["type"] == "api_key_validated"
                assert resp["ok"] is True
                await ws.close()
            finally:
                task.cancel()

    @pytest.mark.asyncio
    async def test_missing_key_fails_actionably(self, fake_keychain: FakeKeychain) -> None:
        FakeProviderClient.raise_on_build = KeychainError("API key not found in keychain.")
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "validate_api_key"})
                assert resp["type"] == "api_key_validated"
                assert resp["ok"] is False
                assert "keychain" in resp["detail"]
                await ws.close()
            finally:
                task.cancel()

    @pytest.mark.asyncio
    async def test_rejected_key_gets_the_auth_failure_copy(
        self, fake_keychain: FakeKeychain
    ) -> None:
        FakeProviderClient.scripted = ProviderError(
            code="auth_failed", message="unauthorized", status_code=401
        )
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "validate_api_key"})
                assert resp["ok"] is False
                assert "keychain set" in resp["detail"]
                await ws.close()
            finally:
                task.cancel()


class FakePresetSaver:
    def __init__(self) -> None:
        self.saved: list[str] = []

    def __call__(self, name: str, path: Path | None = None) -> Path:
        self.saved.append(name)
        return Path("/tmp/fake-config.yaml")


class TestSetPreset:
    @pytest.mark.asyncio
    async def test_valid_preset_persists_and_applies(
        self, fake_keychain: FakeKeychain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saver = FakePresetSaver()
        monkeypatch.setattr("tstd.daemon.save_active_preset", saver)
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "set_preset", "name": "budget"})
                assert saver.saved == ["budget"]
                assert resp["type"] == "setup_state"
                assert resp["active_preset"] == "budget"
                # The daemon instance applies immediately...
                assert daemon.config.active_preset == "budget"
                # ...without leaking into the process-wide cached config.
                assert cached_config().active_preset == "tst-default"
                await ws.close()
            finally:
                task.cancel()

    @pytest.mark.asyncio
    async def test_unknown_preset_is_a_typed_error(
        self, fake_keychain: FakeKeychain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saver = FakePresetSaver()
        monkeypatch.setattr("tstd.daemon.save_active_preset", saver)
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "set_preset", "name": "nope"})
                assert resp["type"] == "error"
                assert resp["code"] == "unknown_preset"
                assert saver.saved == []
                await ws.close()
            finally:
                task.cancel()
