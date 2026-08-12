"""Tests for the OS keychain abstraction.

Uses a mock backend so tests are deterministic and offline.
"""

from __future__ import annotations

import pytest

from tstd.keychain import (
    KeychainBackend,
    KeychainError,
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
