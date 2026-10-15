"""Authorization-code + PKCE against the configured login host (TD-4853).

The token request sends the verifier and no client secret. Error
descriptions from the provider are dropped; only the error code is mapped
to a sentence.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from ..config import MicrosoftConnectorConfig
from .microsoft_http import get_json, post_form
from .microsoft_models import (
    FAILED_MESSAGE,
    REFRESH_FAILED,
    SCOPES,
    MicrosoftError,
    MicrosoftTokens,
    oauth_phase,
    printable_secret,
    public_oauth_message,
)

_AUTHORIZE_TAIL = "/oauth2/v2.0/authorize"
_TOKEN_TAIL = "/oauth2/v2.0/token"
_ME_PATH = "/v1.0/me"
_ADDRESS = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

ClockFn = Callable[[], float]


def _tenant(tenant: str) -> str:
    return quote(tenant, safe="")


def token_path(tenant: str) -> str:
    return f"/{_tenant(tenant)}{_TOKEN_TAIL}"


def authorize_url(
    *,
    login_host: str,
    tenant: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """The browser URL. The host is the configured login host."""
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": SCOPES,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"https://{login_host}/{_tenant(tenant)}{_AUTHORIZE_TAIL}?{query}"


def authorize_host_ok(url: str, login_host: str) -> bool:
    """True when *url* is https on the configured login host and nothing else."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    # urlsplit leaves username as None when the URL has no userinfo.
    return (
        parts.scheme == "https"
        and parts.hostname == login_host
        and not parts.username
        and not parts.password
    )


def tokens_from_payload(
    payload: dict[str, Any],
    *,
    fallback_refresh: str,
    now: float,
) -> MicrosoftTokens:
    """Build tokens. A missing expires_in expires immediately so the next call refreshes."""
    access = payload.get("access_token")
    if not isinstance(access, str) or not printable_secret(access):
        raise MicrosoftError(FAILED_MESSAGE)
    refresh = payload.get("refresh_token")
    if refresh is None:
        refresh = fallback_refresh
    if not isinstance(refresh, str) or (refresh and not printable_secret(refresh)):
        raise MicrosoftError(FAILED_MESSAGE)
    expires = payload.get("expires_in")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires <= 0:
        expires_at = now
    else:
        expires_at = now + float(expires)
    return MicrosoftTokens(access_token=access, refresh_token=refresh, expires_at=expires_at)


def _raise_oauth(payload: dict[str, Any]) -> None:
    error = payload.get("error")
    if isinstance(error, str) and error:
        raise MicrosoftError(public_oauth_message(error), phase=oauth_phase(error))


async def exchange_code(
    *,
    login_host: str,
    tenant: str,
    client_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    timeout_seconds: float,
    transport: Any = None,
    wall: ClockFn | None = None,
) -> MicrosoftTokens:
    """Exchange the authorization code. No client secret is sent."""
    now = wall or time.time
    payload = await post_form(
        login_host,
        token_path(tenant),
        {
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "scope": SCOPES,
        },
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    _raise_oauth(payload)
    return tokens_from_payload(payload, fallback_refresh="", now=now())


async def refresh_tokens(
    *,
    login_host: str,
    tenant: str,
    client_id: str,
    refresh_token: str,
    timeout_seconds: float,
    transport: Any = None,
    wall: ClockFn | None = None,
) -> MicrosoftTokens:
    """Exchange a refresh token. Keep the previous one when the provider omits a new one."""
    if not refresh_token:
        raise MicrosoftError(REFRESH_FAILED, phase="expired")
    now = wall or time.time
    payload = await post_form(
        login_host,
        token_path(tenant),
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": SCOPES,
        },
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    _raise_oauth(payload)
    return tokens_from_payload(payload, fallback_refresh=refresh_token, now=now())


def account_from_profile(payload: dict[str, Any]) -> str | None:
    """The mailbox address, or None. Other profile fields are ignored."""
    mail = payload.get("mail")
    upn = payload.get("userPrincipalName")
    for candidate in (mail, upn):
        if isinstance(candidate, str) and _ADDRESS.fullmatch(candidate.strip()):
            return candidate.strip()
    return None


async def fetch_account(
    cfg: MicrosoftConnectorConfig,
    token: str,
    *,
    transport: Any = None,
) -> str | None:
    """User.Read. A failure leaves the account blank; the token is still stored."""
    payload = await get_json(
        cfg.graph_host,
        _ME_PATH,
        token,
        timeout_seconds=cfg.timeout_seconds,
        transport=transport,
    )
    if payload is None:
        return None
    return account_from_profile(payload)
