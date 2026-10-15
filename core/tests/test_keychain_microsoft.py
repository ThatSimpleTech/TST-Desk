"""Microsoft keychain account (TD-4853). A fake backend only — never the OS."""

from __future__ import annotations

import pytest

import tstd.keychain as kc_mod
from tstd.connectors.microsoft_models import MicrosoftTokens
from tstd.connectors.microsoft_tokens import KeychainTokenStore, encode_tokens
from tstd.keychain import (
    MICROSOFT_OAUTH_ACCOUNT,
    KeychainBackend,
    KeychainError,
    delete_microsoft_oauth,
    get_microsoft_oauth,
    microsoft_oauth_is_stored,
    store_microsoft_oauth,
)

ACCESS = "ms-access-" + "a" * 24
REFRESH = "ms-refresh-" + "b" * 24


class MockKeychain(KeychainBackend):
    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    async def get_secret(self, account: str, service: str = "com.thatsimpletech.tstdesk") -> str:
        key = f"{service}:{account}"
        if key not in self._secrets:
            raise KeychainError("not found")
        return self._secrets[key]

    async def set_secret(
        self, account: str, secret: str, service: str = "com.thatsimpletech.tstdesk"
    ) -> None:
        self._secrets[f"{service}:{account}"] = secret

    async def delete_secret(
        self, account: str, service: str = "com.thatsimpletech.tstdesk"
    ) -> None:
        key = f"{service}:{account}"
        if key not in self._secrets:
            raise KeychainError("missing item")
        del self._secrets[key]


class StickyKeychain(MockKeychain):
    async def has_secret(self, account: str, service: str = "com.thatsimpletech.tstdesk") -> bool:
        del account, service
        return True

    async def delete_secret(
        self, account: str, service: str = "com.thatsimpletech.tstdesk"
    ) -> None:
        del account, service
        raise KeychainError("boom " + ACCESS)


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> MockKeychain:
    mock = MockKeychain()
    monkeypatch.setattr(kc_mod, "_get_backend", lambda: mock)
    return mock


@pytest.mark.asyncio
async def test_blob_round_trip_uses_the_reserved_account(backend: MockKeychain) -> None:
    blob = encode_tokens(MicrosoftTokens(ACCESS, REFRESH, 100.0))
    await store_microsoft_oauth(blob)
    assert MICROSOFT_OAUTH_ACCOUNT == "tst-microsoft-oauth"
    assert await microsoft_oauth_is_stored() is True
    assert await get_microsoft_oauth() == blob
    loaded = await KeychainTokenStore().load()
    assert loaded is not None
    assert loaded.access_token == ACCESS
    assert loaded.refresh_token == REFRESH
    assert ACCESS not in repr(loaded)
    await delete_microsoft_oauth()
    assert await microsoft_oauth_is_stored() is False
    assert list(backend._secrets) == []


@pytest.mark.asyncio
async def test_delete_of_a_missing_item_succeeds(backend: MockKeychain) -> None:
    del backend
    await delete_microsoft_oauth()
    assert await microsoft_oauth_is_stored() is False


@pytest.mark.asyncio
async def test_delete_failure_does_not_echo_the_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kc_mod, "_get_backend", lambda: StickyKeychain())
    with pytest.raises(KeychainError, match="Could not delete the Microsoft sign-in") as exc:
        await delete_microsoft_oauth()
    assert ACCESS not in str(exc.value)


@pytest.mark.asyncio
async def test_store_rejects_a_newline(backend: MockKeychain) -> None:
    del backend
    with pytest.raises(KeychainError):
        await store_microsoft_oauth("line\nbreak")
