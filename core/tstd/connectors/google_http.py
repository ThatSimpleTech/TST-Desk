"""The only Google HTTP client (TD-4854).

Hosts are arguments from ``connectors.google``. Redirects are off so a
bearer token cannot ride a 3xx onto a host the config did not name.
Nothing here logs a body, a header, a token, or the client secret.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..logging import get_logger
from .google_models import FAILED_MESSAGE, GoogleError, scrub

log = get_logger("tstd.connectors.google")

_USER_AGENT = "tst-desk"
_JSON = "application/json"


def _url(host: str, path: str) -> str:
    return f"https://{host}{path}"


def _secret_fields(fields: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        fields.get(key, "") for key in ("code", "code_verifier", "refresh_token", "client_secret")
    )


async def post_form(
    host: str,
    path: str,
    fields: dict[str, str],
    *,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """POST a form to the configured token host. Code exchange and refresh.

    The desktop client secret is one of the fields. It is scrubbed from
    any exception text and never logged.
    """
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout_seconds) as client:
            response = await client.post(
                _url(host, path),
                data=fields,
                headers={"Accept": _JSON, "User-Agent": _USER_AGENT},
                follow_redirects=False,
            )
            payload: Any = None
            if response.content:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
            # The error code is safe. The description can echo a token or the secret.
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                return payload
            if response.is_redirect or response.status_code >= 400 or not isinstance(payload, dict):
                log.warning("google request failed")
                raise GoogleError(FAILED_MESSAGE)
            return payload
    except GoogleError:
        raise
    except httpx.HTTPError as exc:
        log.warning("google request failed")
        raise GoogleError(scrub(str(exc), _secret_fields(fields))) from None


async def post_json(
    host: str,
    path: str,
    payload: bytes,
    token: str,
    *,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """POST JSON. The status is returned; the response body is not."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": _JSON,
        "User-Agent": _USER_AGENT,
    }
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout_seconds) as client:
            response = await client.post(
                _url(host, path),
                content=payload,
                headers=headers,
                follow_redirects=False,
            )
    except httpx.HTTPError as exc:
        log.warning("google request failed")
        raise GoogleError(scrub(str(exc), (token,))) from None
    if response.is_redirect:
        log.warning("google request failed")
        raise GoogleError(FAILED_MESSAGE)
    return response.status_code


async def get_json(
    host: str,
    path: str,
    token: str,
    *,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any] | None:
    """GET JSON. A failure is None — the body is not returned and not logged."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": _JSON,
        "User-Agent": _USER_AGENT,
    }
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout_seconds) as client:
            response = await client.get(
                _url(host, path),
                headers=headers,
                follow_redirects=False,
            )
    except httpx.HTTPError:
        log.warning("google request failed")
        return None
    if response.status_code != 200:
        return None
    try:
        payload: Any = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload
