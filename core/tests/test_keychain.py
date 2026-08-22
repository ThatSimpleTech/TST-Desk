"""Tests for the OS keychain abstraction.

Uses a mock backend so tests are deterministic and offline.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

import tstd.keychain as kc_mod
import tstd.keychain_macos as keychain_macos  # must import off-macOS
import tstd.keychain_windows as keychain_windows  # must import off-Windows
from tstd.keychain import (
    KeychainBackend,
    KeychainError,
    KeychainLockedError,
    MacOSKeychain,
    _classify_cli_failure,
    delete_api_key,
    get_api_key,
    has_keychain_backend,
    store_api_key,
)
from tstd.provider import ProviderClient

# ── Mock backend ────────────────────────────────────────────────────────


class MockKeychain(KeychainBackend):
    """In-memory keychain backend for testing."""

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    async def get_secret(self, account: str, service: str = "com.thatsimpletech.tstdesk") -> str:
        key = f"{service}:{account}"
        if key not in self._secrets:
            raise KeychainError("API key not found in keychain.")
        return self._secrets[key]

    async def set_secret(
        self, account: str, secret: str, service: str = "com.thatsimpletech.tstdesk"
    ) -> None:
        key = f"{service}:{account}"
        self._secrets[key] = secret

    async def delete_secret(
        self, account: str, service: str = "com.thatsimpletech.tstdesk"
    ) -> None:
        key = f"{service}:{account}"
        if key not in self._secrets:
            raise KeychainError("Failed to delete keychain secret.")
        del self._secrets[key]


@pytest.fixture(autouse=True)
def _patch_keychain(monkeypatch: pytest.MonkeyPatch) -> MockKeychain:
    """Replace the real keychain backend with a mock for all tests in this module."""
    mock = MockKeychain()
    # Patch the module-level _get_backend function to return our mock
    import tstd.keychain as kc_mod

    monkeypatch.setattr(kc_mod, "_get_backend", lambda: mock)
    return mock


# ── Tests ───────────────────────────────────────────────────────────────


class TestKeychainCRUD:
    """Test basic create, read, update, delete operations."""

    async def test_store_and_read(self) -> None:
        await store_api_key("sk-test-key-12345")
        key = await get_api_key()
        assert key == "sk-test-key-12345"

    async def test_store_multiple_providers(self) -> None:
        await store_api_key("sk-openrouter-key", provider_name="openrouter")
        await store_api_key("sk-openai-key", provider_name="openai")

        assert await get_api_key("openrouter") == "sk-openrouter-key"
        assert await get_api_key("openai") == "sk-openai-key"

    async def test_delete(self) -> None:
        await store_api_key("sk-to-delete")
        await delete_api_key()

        with pytest.raises(KeychainError, match="not found"):
            await get_api_key()

    async def test_delete_nonexistent_raises(self) -> None:
        with pytest.raises(KeychainError, match="Failed to delete"):
            await delete_api_key("nonexistent")

    async def test_read_nonexistent_raises(self) -> None:
        with pytest.raises(KeychainError, match="not found"):
            await get_api_key("unknown-provider")

    async def test_overwrite(self) -> None:
        """Storing a key that already exists should overwrite."""
        await store_api_key("sk-old-key")
        await store_api_key("sk-new-key")
        assert await get_api_key() == "sk-new-key"


class TestKeychainDetection:
    """Test backend detection."""

    def test_has_backend(self) -> None:
        """On macOS/Linux, a backend should be detectable."""
        # This test runs with the mock installed by autouse; the real
        # has_keychain_backend checks the real platform.
        # We just check the function exists and returns bool.
        result = has_keychain_backend()
        assert isinstance(result, bool)

    async def test_mock_is_used(self, _patch_keychain: MockKeychain) -> None:
        """Verify the mock backend is actually in use."""
        await store_api_key("sk-mock-works")
        assert (
            _patch_keychain._secrets["com.thatsimpletech.tstdesk:tst-openrouter"] == "sk-mock-works"
        )


class TestProviderKeychainIntegration:
    """Test that ProviderClient can load keys from the keychain."""

    async def test_from_keychain(self, _patch_keychain: MockKeychain) -> None:
        """ProviderClient.from_keychain() should load the key from the keychain."""
        await store_api_key("sk-keychain-key")
        client = await ProviderClient.from_keychain(
            base_url="http://test.local/v1",
        )
        assert client.api_key == "sk-keychain-key"
        assert client.base_url == "http://test.local/v1"
        await client.close()

    async def test_from_keychain_custom_provider(self, _patch_keychain: MockKeychain) -> None:
        """from_keychain with a custom provider name."""
        await store_api_key("sk-custom-provider", provider_name="openai")
        client = await ProviderClient.from_keychain(
            base_url="http://test.local/v1",
            provider_name="openai",
        )
        assert client.api_key == "sk-custom-provider"
        await client.close()

    async def test_from_keychain_missing_key(self, _patch_keychain: MockKeychain) -> None:
        """from_keychain should raise KeychainError when no key is stored."""
        with pytest.raises(KeychainError, match="not found"):
            await ProviderClient.from_keychain(base_url="http://test.local/v1")


class TestWindowsBackend:
    """Windows Credential Manager backend (TD-1102), ctypes seam mocked.

    Importing tstd.keychain_windows at module load (above) is itself the
    assertion that the module imports cleanly off Windows.
    """

    async def test_get_decodes_utf16_blob(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            keychain_windows, "_cred_read", lambda target: "sekret".encode("utf-16-le")
        )
        backend = keychain_windows.WindowsCredentialManager()
        assert await backend.get_secret("tst-openrouter") == "sekret"

    async def test_get_strips_a_trailing_terminator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Credentials written by other tools may carry a NUL terminator.
        monkeypatch.setattr(
            keychain_windows,
            "_cred_read",
            lambda target: "sekret\x00".encode("utf-16-le"),
        )
        backend = keychain_windows.WindowsCredentialManager()
        assert await backend.get_secret("tst-openrouter") == "sekret"

    async def test_get_not_found_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(target: str) -> bytes:
            raise KeychainError("API key not found in Windows Credential Manager")

        monkeypatch.setattr(keychain_windows, "_cred_read", _raise)
        backend = keychain_windows.WindowsCredentialManager()
        with pytest.raises(KeychainError, match="not found"):
            await backend.get_secret("tst-openrouter")

    async def test_store_encodes_utf16_under_service_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def _write(target: str, blob: bytes, username: str) -> None:
            captured.update(target=target, blob=blob, username=username)

        monkeypatch.setattr(keychain_windows, "_cred_write", _write)
        backend = keychain_windows.WindowsCredentialManager()
        await backend.set_secret("tst-openrouter", "sekret")
        assert captured["target"] == "com.thatsimpletech.tstdesk:tst-openrouter"
        assert captured["blob"] == "sekret".encode("utf-16-le")
        assert captured["username"] == "tst-openrouter"

    async def test_delete_targets_service_account(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[str] = []
        monkeypatch.setattr(keychain_windows, "_cred_delete", captured.append)
        backend = keychain_windows.WindowsCredentialManager()
        await backend.delete_secret("tst-openrouter")
        assert captured == ["com.thatsimpletech.tstdesk:tst-openrouter"]


class TestMacOSWriteBindings:
    """macOS writes must never put the secret where `ps` can read it (TD-4813).

    `security add-generic-password -w <secret>` exposes the key in argv for
    the lifetime of the call; the write now goes through SecItemAdd via
    ctypes instead. These tests run the real MacOSKeychain.set_secret flow
    with only the process spawns and the framework call faked.
    """

    @pytest.fixture
    def darwin_host(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(kc_mod, "_backend", None)
        # The autouse mock would swallow store_api_key before it reaches
        # MacOSKeychain; these tests need the real macOS flow. Every CLI
        # spawn is faked to succeed and recorded — tests must never touch
        # the developer's actual keychain.
        monkeypatch.setattr(kc_mod, "_get_backend", lambda: kc_mod.MacOSKeychain())
        spawned: list[tuple[str, ...]] = []

        class FakeProc:
            returncode = 0

            async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
                return b"", b""

        async def fake_exec(*argv: str, **_kwargs: Any) -> FakeProc:
            spawned.append(argv)
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return spawned

    async def test_store_routes_through_the_framework_seam(
        self, darwin_host: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[str, str, str]] = []

        def _write(service: str, account: str, secret: str) -> None:
            captured.append((service, account, secret))

        monkeypatch.setattr(keychain_macos, "_sec_add", _write)
        await store_api_key("sk-test-key", provider_name="openrouter")
        assert captured == [("com.thatsimpletech.tstdesk", "tst-openrouter", "sk-test-key")]

    async def test_no_spawned_argv_carries_the_secret(
        self, darwin_host: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The acceptance check in-process. Every process this flow spawns has
        # its argv recorded by the darwin_host fixture; none may contain the
        # API key. Reverting to the CLI add fails this: `-w <secret>` lands in
        # recorded argv exactly where `ps` would have read it.
        monkeypatch.setattr(keychain_macos, "_sec_add", lambda *_args: None)
        await store_api_key("sk-test-guard-key", provider_name="openrouter")
        # Sanity: the pre-clean delete really did pass through here, so this
        # test guards a live path rather than an empty spawn list.
        assert darwin_host, "expected the CLI delete to be intercepted"
        assert "sk-test-guard-key" not in repr(darwin_host)

    def test_locked_keychain_maps_to_locked_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            keychain_macos,
            "_sec_item_add",
            lambda *_args: keychain_macos._ERR_SEC_INTERACTION_NOT_ALLOWED,
        )
        with pytest.raises(KeychainLockedError, match="login keychain is locked"):
            keychain_macos._sec_add("svc", "acct", "sec")

    def test_duplicate_after_unclean_delete_names_the_blocker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            keychain_macos,
            "_sec_item_add",
            lambda *_args: keychain_macos._ERR_SEC_DUPLICATE_ITEM,
        )
        with pytest.raises(KeychainError, match="could not be replaced"):
            keychain_macos._sec_add("svc", "acct", "sec")

    def test_unknown_status_keeps_the_code_visible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(keychain_macos, "_sec_item_add", lambda *_args: -34018)
        with pytest.raises(KeychainError, match="OSStatus -34018"):
            keychain_macos._sec_add("svc", "acct", "sec")


class TestBackendDispatch:
    """_detect_backend picks the platform backend (TD-1102 adds win32)."""

    def test_win32_dispatches_to_credential_manager(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        backend = kc_mod._detect_backend()
        assert isinstance(backend, keychain_windows.WindowsCredentialManager)

    def test_darwin_dispatches_to_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        assert isinstance(kc_mod._detect_backend(), kc_mod.MacOSKeychain)

    def test_linux_dispatches_to_secret_service(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        assert isinstance(kc_mod._detect_backend(), kc_mod.LinuxSecretService)

    def test_unknown_platform_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "plan9")
        with pytest.raises(KeychainError, match="Unsupported platform"):
            kc_mod._detect_backend()


class TestLockedClassification:
    """TD-1105: locked/drifted keychain stderr maps to KeychainLockedError
    with unlock guidance instead of raw CLI output."""

    @pytest.mark.parametrize(
        "stderr",
        [
            # The observed AD-drift failure (2026-08-14).
            "security: SecKeychainItemCreateFromContent: "
            "The user name or passphrase you entered is not correct.",
            "security: SecItemAdd: User interaction is not allowed.",
            "secret-tool: Cannot create an item in a locked collection",
        ],
    )
    def test_locked_markers_classify(self, stderr: str) -> None:
        err = _classify_cli_failure(stderr, "Failed to store keychain secret")
        assert isinstance(err, KeychainLockedError)
        assert "Keychain Access" in str(err)
        assert stderr not in str(err)  # guidance replaces raw stderr

    def test_other_failures_keep_the_raw_stderr(self) -> None:
        err = _classify_cli_failure("weird backend exploded", "Failed to store keychain secret")
        assert type(err) is KeychainError
        assert "weird backend exploded" in str(err)

    async def test_macos_store_raises_locked_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The write goes through SecItemAdd since TD-4813, so a locked
        # keychain surfaces as OSStatus -25308 from the framework rather than
        # CLI stderr; only the pre-clean delete still rides the CLI.
        class _Proc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

        async def _fake_exec(*args: Any, **kwargs: Any) -> _Proc:
            return _Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        monkeypatch.setattr(
            keychain_macos,
            "_sec_item_add",
            lambda *_args: keychain_macos._ERR_SEC_INTERACTION_NOT_ALLOWED,
        )
        with pytest.raises(KeychainLockedError):
            await MacOSKeychain().set_secret("tst-openrouter", "sk-x")
