"""Keychain storage for the Microsoft public-client tokens (TD-4838, TD-4853).

One account, ``tst-microsoft-oauth``, holds a printable-ASCII JSON blob of
the access token, the refresh token, and ``expires_at``. The mailbox
address is not in the blob: reading it back would be a secret read on
every setup_state.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol

from ..config import MicrosoftConnectorConfig
from .microsoft_flow import refresh_tokens
from .microsoft_models import NOT_CONNECTED, MicrosoftError, MicrosoftTokens, printable_secret

_REFRESH_SKEW_SECONDS = 60.0


class TokenStore(Protocol):
    async def load(self) -> MicrosoftTokens | None: ...

    async def save(self, tokens: MicrosoftTokens) -> None: ...

    async def delete(self) -> None: ...

    async def stored(self) -> bool: ...


class MemoryTokenStore:
    """In-memory stand-in. Tests use this so the suite never touches the OS."""

    def __init__(self) -> None:
        self.tokens: MicrosoftTokens | None = None

    async def load(self) -> MicrosoftTokens | None:
        return self.tokens

    async def save(self, tokens: MicrosoftTokens) -> None:
        self.tokens = tokens

    async def delete(self) -> None:
        self.tokens = None

    async def stored(self) -> bool:
        return self.tokens is not None


class KeychainTokenStore:
    """The production store. Imports the keychain inside each call.

    A module-level import would bind the real function before the test
    seam patches ``tstd.keychain``.
    """

    async def load(self) -> MicrosoftTokens | None:
        from ..keychain import KeychainError, get_microsoft_oauth

        try:
            blob = await get_microsoft_oauth()
        except KeychainError:
            return None
        return decode_tokens(blob)

    async def save(self, tokens: MicrosoftTokens) -> None:
        from ..keychain import store_microsoft_oauth

        await store_microsoft_oauth(encode_tokens(tokens))

    async def delete(self) -> None:
        from ..keychain import delete_microsoft_oauth

        await delete_microsoft_oauth()

    async def stored(self) -> bool:
        from ..keychain import KeychainError, microsoft_oauth_is_stored

        try:
            return await microsoft_oauth_is_stored()
        except (KeychainError, FileNotFoundError):
            return False


def encode_tokens(tokens: MicrosoftTokens) -> str:
    """JSON the keychain can round-trip. Rejects anything that is not printable ASCII."""
    if not printable_secret(tokens.access_token):
        raise MicrosoftError("Microsoft sign-in failed.")
    if tokens.refresh_token and not printable_secret(tokens.refresh_token):
        raise MicrosoftError("Microsoft sign-in failed.")
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


def decode_tokens(blob: str) -> MicrosoftTokens:
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        raise MicrosoftError(
            "Microsoft sign-in could not be read. Sign in again in Settings."
        ) from None
    if not isinstance(data, dict):
        raise MicrosoftError("Microsoft sign-in could not be read. Sign in again in Settings.")
    access = data.get("access_token")
    refresh = data.get("refresh_token", "")
    expires = data.get("expires_at")
    if not isinstance(access, str) or not printable_secret(access):
        raise MicrosoftError("Microsoft sign-in could not be read. Sign in again in Settings.")
    if not isinstance(refresh, str) or (refresh and not printable_secret(refresh)):
        raise MicrosoftError("Microsoft sign-in could not be read. Sign in again in Settings.")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        raise MicrosoftError("Microsoft sign-in could not be read. Sign in again in Settings.")
    return MicrosoftTokens(access_token=access, refresh_token=refresh, expires_at=float(expires))


def needs_refresh(tokens: MicrosoftTokens, now: float) -> bool:
    return now >= tokens.expires_at - _REFRESH_SKEW_SECONDS


async def ensure_access(
    cfg: MicrosoftConnectorConfig,
    store: TokenStore,
    *,
    now: Callable[[], float] | None = None,
    force: bool = False,
    transport: Any = None,
) -> str:
    """Return a usable access token, refreshing when it is inside the skew window."""
    clock = now or time.time
    tokens = await store.load()
    if tokens is None:
        raise MicrosoftError(NOT_CONNECTED)
    if not force and not needs_refresh(tokens, clock()):
        return tokens.access_token
    if not cfg.enabled():
        from .microsoft_models import OFF_MESSAGE

        raise MicrosoftError(OFF_MESSAGE, phase="off")
    refreshed = await refresh_tokens(
        login_host=cfg.login_host,
        tenant=cfg.tenant,
        client_id=cfg.client_id,
        refresh_token=tokens.refresh_token,
        timeout_seconds=cfg.timeout_seconds,
        transport=transport,
        wall=clock,
    )
    await store.save(refreshed)
    return refreshed.access_token
