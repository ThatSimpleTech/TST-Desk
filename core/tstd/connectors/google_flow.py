"""Installed-app authorization code + PKCE against the configured hosts (TD-4854).

The token request sends the verifier and the desktop client secret.
Error descriptions from the provider are dropped; only the error code
is mapped to a sentence. ``access_type=offline`` and ``prompt=consent``
are what make Google return a refresh token to a desktop client.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from ..config import GoogleConnectorConfig
from .google_http import get_json, post_form
from .google_models import (
    EXPIRED_MESSAGE,
    FAILED_MESSAGE,
    SCOPES,
    GoogleError,
    GoogleTokens,
    oauth_phase,
    printable_secret,
    public_oauth_message,
)

_AUTH_PATH = "/o/oauth2/v2/auth"
_TOKEN_PATH = "/token"
_USERINFO_PATH = "/v1/userinfo"
_ADDRESS = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

ClockFn = Callable[[], float]


@dataclass(frozen=True)
class Granted:
    """Tokens plus the account email, if the id token carried one.

    The id token itself is not kept. It is a credential.
    """

    tokens: GoogleTokens
    account: str | None

    def __repr__(self) -> str:
        return f"Granted(account={self.account!r}, tokens={self.tokens!r})"


def authorize_url(
    *,
    auth_host: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """The browser URL. The host is the configured auth host. No client secret."""
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return f"https://{auth_host}{_AUTH_PATH}?{query}"


def authorize_host_ok(url: str, auth_host: str) -> bool:
    """True when *url* is https on the configured auth host and nothing else."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == auth_host
        and not parts.username
        and not parts.password
    )


def email_from_id_token(token: object) -> str | None:
    """The email claim, or None. The token is not verified and not stored.

    It arrived on the token response from the configured host, over TLS.
    A bad payload is ignored. The exception text can quote the token, so
    it is not re-raised.
    """
    if not isinstance(token, str) or token.count(".") < 2:
        return None
    payload = token.split(".", 2)[1]
    pad = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + pad)
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("email_verified") is False:
        return None
    email = data.get("email")
    if isinstance(email, str) and _ADDRESS.fullmatch(email.strip()):
        return email.strip()
    return None


def tokens_from_payload(
    payload: dict[str, Any],
    *,
    fallback_refresh: str,
    now: float,
) -> GoogleTokens:
    """Build tokens. A missing expires_in expires immediately so the next call refreshes."""
    access = payload.get("access_token")
    if not isinstance(access, str) or not printable_secret(access):
        raise GoogleError(FAILED_MESSAGE)
    refresh = payload.get("refresh_token")
    if refresh is None:
        refresh = fallback_refresh
    if not isinstance(refresh, str) or (refresh and not printable_secret(refresh)):
        raise GoogleError(FAILED_MESSAGE)
    expires = payload.get("expires_in")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires <= 0:
        expires_at = now
    else:
        expires_at = now + float(expires)
    return GoogleTokens(access_token=access, refresh_token=refresh, expires_at=expires_at)


def _raise_oauth(payload: dict[str, Any]) -> None:
    error = payload.get("error")
    if isinstance(error, str) and error:
        raise GoogleError(public_oauth_message(error), phase=oauth_phase(error))


async def exchange_code(
    *,
    token_host: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    timeout_seconds: float,
    transport: Any = None,
    wall: ClockFn | None = None,
) -> Granted:
    """Exchange the authorization code. The desktop client secret is sent."""
    if not client_secret:
        from .google_models import SECRET_MESSAGE

        raise GoogleError(SECRET_MESSAGE)
    now = wall or time.time
    payload = await post_form(
        token_host,
        _TOKEN_PATH,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    _raise_oauth(payload)
    tokens = tokens_from_payload(payload, fallback_refresh="", now=now())
    if not tokens.refresh_token:
        raise GoogleError(EXPIRED_MESSAGE, phase="expired")
    return Granted(tokens=tokens, account=email_from_id_token(payload.get("id_token")))


async def refresh_tokens(
    *,
    token_host: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    timeout_seconds: float,
    transport: Any = None,
    wall: ClockFn | None = None,
) -> GoogleTokens:
    """Exchange a refresh token. Keep the previous one when Google omits a new one."""
    if not client_secret:
        from .google_models import SECRET_MESSAGE

        raise GoogleError(SECRET_MESSAGE)
    if not refresh_token:
        raise GoogleError(EXPIRED_MESSAGE, phase="expired")
    now = wall or time.time
    payload = await post_form(
        token_host,
        _TOKEN_PATH,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    _raise_oauth(payload)
    return tokens_from_payload(payload, fallback_refresh=refresh_token, now=now())


def account_from_profile(payload: dict[str, Any]) -> str | None:
    """The mailbox address, or None. Other profile fields are ignored."""
    if payload.get("email_verified") is False:
        return None
    email = payload.get("email")
    if isinstance(email, str) and _ADDRESS.fullmatch(email.strip()):
        return email.strip()
    return None


async def fetch_account(
    cfg: GoogleConnectorConfig,
    token: str,
    *,
    transport: Any = None,
) -> str | None:
    """OpenID userinfo. A failure leaves the account blank; the token is still stored."""
    payload = await get_json(
        cfg.userinfo_host,
        _USERINFO_PATH,
        token,
        timeout_seconds=cfg.timeout_seconds,
        transport=transport,
    )
    if payload is None:
        return None
    return account_from_profile(payload)
