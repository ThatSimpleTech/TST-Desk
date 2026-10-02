"""Keychain storage for the GitHub user-to-server token (TD-4838, TD-4852).

One account, ``tst-github-oauth``, holds a printable-ASCII JSON blob of
the access token, the refresh token, and ``expires_at``. Presence uses
``has_secret``. Login is not in the blob: reading it back would be a
secret read on every setup_state.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from ..config import GithubConnectorConfig
from .github_flow import refresh_tokens
from .github_models import GithubError, GithubTokens, printable_secret

_REFRESH_SKEW_SECONDS = 60.0


class TokenStore(Protocol):
    async def load(self) -> GithubTokens | None: ...

    async def save(self, tokens: GithubTokens) -> None: ...

    async def delete(self) -> None: ...

    async def stored(self) -> bool: ...


class MemoryTokenStore:
    """In-memory stand-in. Tests use this so the suite never touches the OS."""

    def __init__(self) -> None:
        self.tokens: GithubTokens | None = None

    async def load(self) -> GithubTokens | None:
        return self.tokens

    async def save(self, tokens: GithubTokens) -> None:
        self.tokens = tokens

    async def delete(self) -> None:
        self.tokens = None

    async def stored(self) -> bool:
        return self.tokens is not None


class KeychainTokenStore:
    """The production store. Imports the keychain inside each call.

    A module-level import would bind the real function before the test
    seam patches ``tstd.keychain`` (TD-1105).
    """

    async def load(self) -> GithubTokens | None:
        from ..keychain import KeychainError, get_github_oauth

        try:
            blob = await get_github_oauth()
        except KeychainError:
            return None
        return decode_tokens(blob)

    async def save(self, tokens: GithubTokens) -> None:
        from ..keychain import store_github_oauth

        await store_github_oauth(encode_tokens(tokens))

    async def delete(self) -> None:
        from ..keychain import delete_github_oauth

        await delete_github_oauth()

    async def stored(self) -> bool:
        from ..keychain import KeychainError, github_oauth_is_stored

        try:
            return await github_oauth_is_stored()
        except (KeychainError, FileNotFoundError):
            return False


def encode_tokens(tokens: GithubTokens) -> str:
    """JSON the keychain can round-trip. Rejects anything that is not printable ASCII."""
    if not printable_secret(tokens.access_token):
        raise GithubError("GitHub sign-in failed.")
    if tokens.refresh_token and not printable_secret(tokens.refresh_token):
        raise GithubError("GitHub sign-in failed.")
    blob = json.dumps(
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at,
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
    )
    from ..keychain import _reject_api_key_value

    _reject_api_key_value(blob)
    return blob


def decode_tokens(blob: str) -> GithubTokens:
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        raise GithubError("GitHub sign-in could not be read. Connect again in Settings.") from None
    if not isinstance(data, dict):
        raise GithubError("GitHub sign-in could not be read. Connect again in Settings.")
    access = data.get("access_token")
    refresh = data.get("refresh_token", "")
    expires = data.get("expires_at")
    if not isinstance(access, str) or not printable_secret(access):
        raise GithubError("GitHub sign-in could not be read. Connect again in Settings.")
    if not isinstance(refresh, str) or (refresh and not printable_secret(refresh)):
        raise GithubError("GitHub sign-in could not be read. Connect again in Settings.")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        raise GithubError("GitHub sign-in could not be read. Connect again in Settings.")
    return GithubTokens(access_token=access, refresh_token=refresh, expires_at=float(expires))


def needs_refresh(tokens: GithubTokens, now: float) -> bool:
    return now >= tokens.expires_at - _REFRESH_SKEW_SECONDS


async def ensure_access(
    cfg: GithubConnectorConfig,
    store: TokenStore,
    *,
    now: Callable[[], float] | None = None,
    force: bool = False,
    transport: Any = None,
) -> str:
    """Return a usable access token, refreshing when it is inside the skew window."""
    import time

    clock = now or time.time
    tokens = await store.load()
    if tokens is None:
        raise GithubError("GitHub is not connected. Connect in Settings.")
    if not force and not needs_refresh(tokens, clock()):
        return tokens.access_token
    if not cfg.enabled():
        from .github_models import OFF_MESSAGE

        raise GithubError(OFF_MESSAGE, phase="off")
    refreshed = await refresh_tokens(
        client_id=cfg.client_id,
        web_host=cfg.web_host,
        refresh_token=tokens.refresh_token,
        timeout_seconds=cfg.timeout_seconds,
        transport=transport,
        wall=clock,
    )
    await store.save(refreshed)
    return refreshed.access_token
