"""The only GitHub HTTP client.

Hosts are arguments from ``connectors.github``. Redirects are off so a
token cannot ride a 3xx onto a host the config did not name. Nothing
here logs a body, a header, or a token (TD-4852).
"""

from __future__ import annotations

from typing import Any

import httpx

from ..logging import get_logger
from .github_models import GithubError

log = get_logger("tstd.connectors.github")

_API_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_USER_AGENT = "tst-desk"
_DEVICE_ACCEPT = "application/json"


def _url(host: str, path: str) -> str:
    return f"https://{host}{path}"


async def post_form(
    host: str,
    path: str,
    fields: dict[str, str],
    *,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """POST a form to the configured web host. Device flow and refresh."""
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout_seconds) as client:
            response = await client.post(
                _url(host, path),
                data=fields,
                headers={"Accept": _DEVICE_ACCEPT, "User-Agent": _USER_AGENT},
                follow_redirects=False,
            )
            # Device-flow errors often arrive as HTTP 400 with an error code
            # and a description that can echo a token. Return the code; never
            # log the body.
            payload: Any = None
            if response.content:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                return payload
            if response.status_code >= 400 or not isinstance(payload, dict):
                log.warning("github request failed")
                raise GithubError("GitHub request failed.")
            return payload
    except GithubError:
        raise
    except httpx.HTTPError:
        log.warning("github request failed")
        raise GithubError("GitHub request failed.") from None


async def api_json(
    host: str,
    method: str,
    path: str,
    token: str,
    *,
    timeout_seconds: float,
    body: dict[str, Any] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[int, Any]:
    """Call the configured API host. The token stays in the header."""
    headers = {
        "Accept": _API_ACCEPT,
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": _API_VERSION,
        "Authorization": f"Bearer {token}",
    }
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout_seconds) as client:
            response = await client.request(
                method,
                _url(host, path),
                headers=headers,
                json=body,
                follow_redirects=False,
            )
    except httpx.HTTPError:
        log.warning("github request failed")
        raise GithubError("GitHub request failed.") from None
    if response.status_code == 204 or not response.content:
        return response.status_code, None
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload
