"""Google keychain accounts (TD-4854). A fake backend only — never the OS."""

from __future__ import annotations

import pytest

import tstd.keychain as kc_mod
from tstd.connectors.google_models import GoogleTokens
from tstd.connectors.google_tokens import KeychainSecretStore, KeychainTokenStore, encode_tokens
from tstd.keychain import (
    GOOGLE_CLIENT_SECRET_ACCOUNT,
    GOOGLE_OAUTH_ACCOUNT,
    KeychainBackend,
    KeychainError,
    delete_google_oauth,
    get_google_client_secret,
    get_google_oauth,
    google_client_secret_is_stored,
    google_oauth_is_stored,
    store_google_client_secret,
    store_google_oauth,
)

ACCESS = "gg-access-" + "a" * 24
REFRESH = "gg-refresh-" + "b" * 24
SECRET = "gg-secret-" + "s" * 24


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
async def test_blob_and_secret_use_different_accounts(backend: MockKeychain) -> None:
    blob = encode_tokens(GoogleTokens(ACCESS, REFRESH, 100.0))
    await store_google_oauth(blob)
    await store_google_client_secret(SECRET)
    assert GOOGLE_OAUTH_ACCOUNT == "tst-google-oauth"
    assert GOOGLE_CLIENT_SECRET_ACCOUNT == "tst-google-client-secret"
    assert await google_oauth_is_stored() is True
    assert await google_client_secret_is_stored() is True
    assert await get_google_oauth() == blob
    assert await get_google_client_secret() == SECRET
    loaded = await KeychainTokenStore().load()
    assert loaded is not None
    assert loaded.access_token == ACCESS
    assert loaded.refresh_token == REFRESH
    assert ACCESS not in repr(loaded)
    assert await KeychainSecretStore().load() == SECRET
    await delete_google_oauth()
    assert await google_oauth_is_stored() is False
    assert await google_client_secret_is_stored() is True
    assert await get_google_client_secret() == SECRET
    assert list(backend._secrets) == [f"com.thatsimpletech.tstdesk:{GOOGLE_CLIENT_SECRET_ACCOUNT}"]


@pytest.mark.asyncio
async def test_delete_of_a_missing_item_succeeds(backend: MockKeychain) -> None:
    del backend
    await delete_google_oauth()
    assert await google_oauth_is_stored() is False


@pytest.mark.asyncio
async def test_delete_failure_does_not_echo_the_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kc_mod, "_get_backend", lambda: StickyKeychain())
    with pytest.raises(KeychainError, match="Could not delete the Google sign-in") as exc:
        await delete_google_oauth()
    assert ACCESS not in str(exc.value)


@pytest.mark.asyncio
async def test_store_rejects_a_newline(backend: MockKeychain) -> None:
    del backend
    with pytest.raises(KeychainError):
        await store_google_oauth("line\nbreak")
    with pytest.raises(KeychainError):
        await store_google_client_secret("line\nbreak")
