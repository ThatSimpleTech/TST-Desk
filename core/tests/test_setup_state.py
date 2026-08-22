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
import contextlib
import json
import tempfile
from pathlib import Path
from typing import Any, ClassVar

import pytest
from websockets.asyncio.client import connect

from tstd.config import cached_config
from tstd.daemon import Daemon
from tstd.keychain import KeychainError, KeychainLockedError
from tstd.protocol import PROTOCOL_VERSION, DeleteApiKey, ValidateApiKey, parse_client_message
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


async def _stop_daemon(task: asyncio.Task[Any]) -> None:
    """Cancel the daemon task and wait for its shutdown to finish.

    The daemon holds audit.db open until _shutdown() closes the audit
    store; on Windows the surrounding TemporaryDirectory cleanup cannot
    unlink an open file, so teardown must complete here, not race it.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


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

    async def delete(self, provider_name: str = "openrouter") -> None:
        if provider_name not in self.stored:
            raise KeychainError(f"API key not found in keychain for {provider_name!r}.")
        del self.stored[provider_name]

    def fail_delete(self, e: Exception) -> None:
        async def _fail(provider_name: str = "openrouter") -> None:
            raise e

        self.delete = _fail  # type: ignore[method-assign]


class FakeProviderClient:
    """Class-level stand-in: instances' chat_completion returns a scripted
    result.  The direct constructor records the key it was built with, so
    tests can prove the typed-key path bypasses the keychain (TD-1106)."""

    scripted: Any = object()  # non-ProviderError ⇒ success
    raise_on_build: Exception | None = None
    built_with: ClassVar[list[str | None]] = []

    def __init__(self, base_url: str = "", api_key: str | None = None, **kwargs: Any) -> None:
        type(self).built_with.append(api_key)

    @classmethod
    async def from_keychain(cls, base_url: str, **kwargs: Any) -> FakeProviderClient:
        if cls.raise_on_build is not None:
            raise cls.raise_on_build
        return cls()

    async def chat_completion(self, request: Any) -> Any:
        return self.scripted

    @classmethod
    def reset(cls) -> None:
        cls.scripted = object()
        cls.raise_on_build = None
        cls.built_with = []


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

    async def _delete(provider_name: str = "openrouter") -> None:
        await fk.delete(provider_name)

    monkeypatch.setattr("tstd.daemon.get_api_key", _get)
    monkeypatch.setattr("tstd.daemon.store_api_key", _store)
    monkeypatch.setattr("tstd.daemon.delete_api_key", _delete)
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
                assert {"tst-default", "budget", "local", "vllm"} <= set(resp["presets"])
                await ws.close()
            finally:
                await _stop_daemon(task)

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
                await _stop_daemon(task)


class TestSetApiKey:
    @pytest.mark.asyncio
    async def test_store_then_ack(self, fake_keychain: FakeKeychain) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(
                    ws,
                    {"type": "set_api_key", "api_key": "sk-live-check-12345"},  # tst-secret-ok
                )
                assert fake_keychain.stored["openrouter"] == "sk-live-check-12345"  # tst-secret-ok
                assert resp["type"] == "setup_state"
                assert resp["has_api_key"] is True
                # The key is never echoed anywhere on the wire.
                assert "sk-live-check-12345" not in json.dumps(resp)  # tst-secret-ok
                await ws.close()
            finally:
                await _stop_daemon(task)

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
                await _stop_daemon(task)

    @pytest.mark.asyncio
    async def test_locked_keychain_gets_the_locked_code(self, fake_keychain: FakeKeychain) -> None:
        # TD-1105: a locked/drifted keychain surfaces its own code so the
        # UI shows unlock guidance with a retry path, not a dead end.
        fake_keychain.fail_store(KeychainLockedError("The login keychain is locked — …"))
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "set_api_key", "api_key": "sk-x"})
                assert resp["type"] == "error"
                assert resp["code"] == "keychain_locked"
                assert "locked" in resp["message"]
                await ws.close()
            finally:
                await _stop_daemon(task)


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
                await _stop_daemon(task)

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
                await _stop_daemon(task)

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
                assert "title bar" in resp["detail"]
                await ws.close()
            finally:
                await _stop_daemon(task)

    @pytest.mark.asyncio
    async def test_typed_key_validated_without_keychain(self, fake_keychain: FakeKeychain) -> None:
        # TD-1106: Validate checks the key typed in the field, regardless
        # of keychain state — the keychain path is rigged to explode, so a
        # pass here proves the typed key never touches it (a failed or
        # skipped store can never dead-end the step).
        FakeProviderClient.raise_on_build = KeychainError("keychain exploded")
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "validate_api_key", "api_key": "sk-typed-directly"})
                assert resp["type"] == "api_key_validated"
                assert resp["ok"] is True
                assert FakeProviderClient.built_with == ["sk-typed-directly"]
                assert fake_keychain.stored == {}
                await ws.close()
            finally:
                await _stop_daemon(task)

    @pytest.mark.asyncio
    async def test_typed_key_rejection_gets_the_auth_failure_copy(
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
                resp = await _ask(ws, {"type": "validate_api_key", "api_key": "sk-bad"})
                assert resp["ok"] is False
                assert "re-enter a valid API key" in resp["detail"]
                await ws.close()
            finally:
                await _stop_daemon(task)

    def test_message_parses_with_and_without_key(self) -> None:
        with_key = parse_client_message('{"type": "validate_api_key", "api_key": "sk-x"}')
        assert isinstance(with_key, ValidateApiKey)
        assert with_key.api_key == "sk-x"
        bare = parse_client_message('{"type": "validate_api_key"}')
        assert isinstance(bare, ValidateApiKey)
        assert bare.api_key is None


class TestDeleteApiKey:
    """TD-1102: the key is removable, acked by a fresh setup_state."""

    @pytest.mark.asyncio
    async def test_delete_then_ack(self, fake_keychain: FakeKeychain) -> None:
        fake_keychain.stored["openrouter"] = "sk-to-remove"
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "delete_api_key"})
                assert fake_keychain.stored == {}
                assert resp["type"] == "setup_state"
                assert resp["has_api_key"] is False
                await ws.close()
            finally:
                task.cancel()

    @pytest.mark.asyncio
    async def test_delete_nonexistent_is_a_typed_error(self, fake_keychain: FakeKeychain) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "delete_api_key"})
                assert resp["type"] == "error"
                assert resp["code"] == "key_delete_failed"
                await ws.close()
            finally:
                task.cancel()

    @pytest.mark.asyncio
    async def test_delete_failure_is_a_typed_error(self, fake_keychain: FakeKeychain) -> None:
        fake_keychain.stored["openrouter"] = "sk-stuck"
        fake_keychain.fail_delete(KeychainError("keychain locked"))
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "delete_api_key"})
                assert resp["type"] == "error"
                assert resp["code"] == "key_delete_failed"
                # A failed delete leaves the key in place.
                assert fake_keychain.stored["openrouter"] == "sk-stuck"
                await ws.close()
            finally:
                task.cancel()

    @pytest.mark.asyncio
    async def test_delete_custom_provider_leaves_default(self, fake_keychain: FakeKeychain) -> None:
        fake_keychain.stored["openrouter"] = "sk-default"
        fake_keychain.stored["openai"] = "sk-other"
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                resp = await _ask(ws, {"type": "delete_api_key", "provider": "openai"})
                assert fake_keychain.stored == {"openrouter": "sk-default"}
                # has_api_key tracks the default provider, which is untouched.
                assert resp["has_api_key"] is True
                await ws.close()
            finally:
                task.cancel()

    def test_delete_parses_with_default_provider(self) -> None:
        msg = parse_client_message('{"type": "delete_api_key"}')
        assert isinstance(msg, DeleteApiKey)
        assert msg.provider == "openrouter"


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
                await _stop_daemon(task)

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
                await _stop_daemon(task)
