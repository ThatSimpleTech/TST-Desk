"""GitHub keychain account (TD-4852). A fake backend only — never the OS."""

from __future__ import annotations

import pytest

import tstd.keychain as kc_mod
from tstd.connectors.github_models import GithubTokens
from tstd.connectors.github_tokens import KeychainTokenStore, encode_tokens
from tstd.keychain import (
    GITHUB_OAUTH_ACCOUNT,
    KeychainBackend,
    KeychainError,
    delete_github_oauth,
    get_github_oauth,
    github_oauth_is_stored,
    store_github_oauth,
)

ACCESS = "ghu_" + "a" * 24
REFRESH = "ghr_" + "b" * 24


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
    blob = encode_tokens(GithubTokens(ACCESS, REFRESH, 100.0))
    await store_github_oauth(blob)
    assert GITHUB_OAUTH_ACCOUNT == "tst-github-oauth"
    assert await github_oauth_is_stored() is True
    assert await get_github_oauth() == blob
    loaded = await KeychainTokenStore().load()
    assert loaded is not None
    assert loaded.access_token == ACCESS
    assert loaded.refresh_token == REFRESH
    await delete_github_oauth()
    assert await github_oauth_is_stored() is False
    assert list(backend._secrets) == []


@pytest.mark.asyncio
async def test_delete_of_a_missing_item_succeeds(backend: MockKeychain) -> None:
    del backend
    await delete_github_oauth()
    assert await github_oauth_is_stored() is False


@pytest.mark.asyncio
async def test_delete_failure_does_not_echo_the_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kc_mod, "_get_backend", lambda: StickyKeychain())
    with pytest.raises(KeychainError, match="Could not delete the GitHub sign-in") as exc:
        await delete_github_oauth()
    assert ACCESS not in str(exc.value)


@pytest.mark.asyncio
async def test_store_rejects_a_newline(backend: MockKeychain) -> None:
    del backend
    with pytest.raises(KeychainError):
        await store_github_oauth("line\nbreak")
