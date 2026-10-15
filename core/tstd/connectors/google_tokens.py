"""Keychain storage for the Google desktop client (TD-4838, TD-4854).

Two accounts. ``tst-google-client-secret`` is the desktop client secret
the user typed in Settings. ``tst-google-oauth`` is a printable-ASCII
JSON blob of the access token, the refresh token, and ``expires_at``.
The mailbox address is not in the blob: reading it back would be a
secret read on every setup_state. Disconnect deletes the token blob and
leaves the client secret, which is app configuration, not a grant.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol

from ..config import GoogleConnectorConfig
from .google_expiry import clear_expired, mark_expired
from .google_flow import refresh_tokens
from .google_models import (
    NOT_CONNECTED,
    OFF_MESSAGE,
    GoogleError,
    GoogleTokens,
    printable_secret,
)

_REFRESH_SKEW_SECONDS = 60.0


class TokenStore(Protocol):
    async def load(self) -> GoogleTokens | None: ...

    async def save(self, tokens: GoogleTokens) -> None: ...

    async def delete(self) -> None: ...

    async def stored(self) -> bool: ...


class SecretStore(Protocol):
    async def load(self) -> str: ...

    async def save(self, secret: str) -> None: ...

    async def stored(self) -> bool: ...


class MemoryTokenStore:
    """In-memory stand-in. Tests use this so the suite never touches the OS."""

    def __init__(self) -> None:
        self.tokens: GoogleTokens | None = None

    async def load(self) -> GoogleTokens | None:
        return self.tokens

    async def save(self, tokens: GoogleTokens) -> None:
        self.tokens = tokens

    async def delete(self) -> None:
        self.tokens = None

    async def stored(self) -> bool:
        return self.tokens is not None


class MemorySecretStore:
    """In-memory client secret. Tests use this so the suite never touches the OS."""

    def __init__(self, secret: str = "") -> None:
        self.secret = secret

    async def load(self) -> str:
        return self.secret

    async def save(self, secret: str) -> None:
        self.secret = secret

    async def stored(self) -> bool:
        return bool(self.secret)


class KeychainTokenStore:
    """The production token store. Imports the keychain inside each call.

    A module-level import would bind the real function before the test
    seam patches ``tstd.keychain``.
    """

    async def load(self) -> GoogleTokens | None:
        from ..keychain import KeychainError, get_google_oauth

        try:
            blob = await get_google_oauth()
        except KeychainError:
            return None
        return decode_tokens(blob)

    async def save(self, tokens: GoogleTokens) -> None:
        from ..keychain import store_google_oauth

        await store_google_oauth(encode_tokens(tokens))

    async def delete(self) -> None:
        from ..keychain import delete_google_oauth

        await delete_google_oauth()

    async def stored(self) -> bool:
        from ..keychain import KeychainError, google_oauth_is_stored

        try:
            return await google_oauth_is_stored()
        except (KeychainError, FileNotFoundError):
            return False


class KeychainSecretStore:
    """The production client-secret store. Same import rule as the token store."""

    async def load(self) -> str:
        from ..keychain import KeychainError, get_google_client_secret

        try:
            return await get_google_client_secret()
        except KeychainError:
            return ""

    async def save(self, secret: str) -> None:
        from ..keychain import store_google_client_secret

        await store_google_client_secret(secret)

    async def stored(self) -> bool:
        from ..keychain import KeychainError, google_client_secret_is_stored

        try:
            return await google_client_secret_is_stored()
        except (KeychainError, FileNotFoundError):
            return False


def encode_tokens(tokens: GoogleTokens) -> str:
    """JSON the keychain can round-trip. Rejects anything that is not printable ASCII."""
    if not printable_secret(tokens.access_token):
        raise GoogleError("Google sign-in failed.")
    if tokens.refresh_token and not printable_secret(tokens.refresh_token):
        raise GoogleError("Google sign-in failed.")
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


def decode_tokens(blob: str) -> GoogleTokens:
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        raise GoogleError("Google sign-in could not be read. Sign in again in Settings.") from None
    if not isinstance(data, dict):
        raise GoogleError("Google sign-in could not be read. Sign in again in Settings.")
    access = data.get("access_token")
    refresh = data.get("refresh_token", "")
    expires = data.get("expires_at")
    if not isinstance(access, str) or not printable_secret(access):
        raise GoogleError("Google sign-in could not be read. Sign in again in Settings.")
    if not isinstance(refresh, str) or (refresh and not printable_secret(refresh)):
        raise GoogleError("Google sign-in could not be read. Sign in again in Settings.")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        raise GoogleError("Google sign-in could not be read. Sign in again in Settings.")
    return GoogleTokens(access_token=access, refresh_token=refresh, expires_at=float(expires))


def needs_refresh(tokens: GoogleTokens, now: float) -> bool:
    return now >= tokens.expires_at - _REFRESH_SKEW_SECONDS


async def ensure_access(
    cfg: GoogleConnectorConfig,
    store: TokenStore,
    secrets: SecretStore,
    *,
    now: Callable[[], float] | None = None,
    force: bool = False,
    transport: Any = None,
) -> str:
    """Return a usable access token, refreshing when it is inside the skew window.

    ``invalid_grant`` marks the connection expired before the error propagates,
    so Settings says reconnect instead of staying on connected.
    """
    clock = now or time.time
    tokens = await store.load()
    if tokens is None:
        raise GoogleError(NOT_CONNECTED)
    if not force and not needs_refresh(tokens, clock()):
        return tokens.access_token
    if not cfg.enabled():
        raise GoogleError(OFF_MESSAGE, phase="off")
    secret = await secrets.load()
    try:
        refreshed = await refresh_tokens(
            token_host=cfg.token_host,
            client_id=cfg.client_id,
            client_secret=secret,
            refresh_token=tokens.refresh_token,
            timeout_seconds=cfg.timeout_seconds,
            transport=transport,
            wall=clock,
        )
    except GoogleError as exc:
        if exc.phase == "expired":
            await mark_expired(exc.message)
        raise
    await store.save(refreshed)
    clear_expired()
    return refreshed.access_token
