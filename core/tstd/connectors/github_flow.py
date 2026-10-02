"""GitHub device-flow state machine.

Sleep and the clock are injected so a test can run pending, slow_down,
and expiry without waiting. The device code stays in this process.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from .github_http import post_form
from .github_models import (
    DeviceCode,
    GithubError,
    GithubTokens,
    check_user_code,
    check_verification_uri,
    oauth_phase,
    positive_int,
    printable_secret,
    public_oauth_message,
)

SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], float]
PostFn = Callable[[str, str, dict[str, str]], Awaitable[dict[str, Any]]]

_DEVICE_PATH = "/login/device/code"
_TOKEN_PATH = "/login/oauth/access_token"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


async def _asleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def parse_device_code(payload: dict[str, Any], web_host: str) -> DeviceCode:
    """Validate the start response. A bad code is a failed sign-in, not a retry."""
    error = payload.get("error")
    if isinstance(error, str) and error:
        raise GithubError(public_oauth_message(error), phase=oauth_phase(error))
    device = payload.get("device_code")
    if not isinstance(device, str) or not printable_secret(device):
        raise GithubError("GitHub sign-in failed.")
    expires = positive_int(payload.get("expires_in"))
    if expires is None:
        raise GithubError("GitHub sign-in failed.")
    interval = positive_int(payload.get("interval")) or 5
    return DeviceCode(
        device_code=device,
        user_code=check_user_code(payload.get("user_code")),
        verification_uri=check_verification_uri(payload.get("verification_uri"), web_host),
        expires_in=expires,
        interval=max(interval, 1),
    )


def tokens_from_payload(
    payload: dict[str, Any],
    *,
    fallback_refresh: str,
    now: float,
) -> GithubTokens:
    """Build tokens. A missing expires_in expires immediately so the next call refreshes."""
    access = payload.get("access_token")
    if not isinstance(access, str) or not printable_secret(access):
        raise GithubError("GitHub sign-in failed.")
    refresh = payload.get("refresh_token")
    if refresh is None:
        refresh = fallback_refresh
    if not isinstance(refresh, str) or (refresh and not printable_secret(refresh)):
        raise GithubError("GitHub sign-in failed.")
    expires = positive_int(payload.get("expires_in"))
    expires_at = now + float(expires) if expires is not None else now
    return GithubTokens(access_token=access, refresh_token=refresh, expires_at=expires_at)


async def start_device_flow(
    *,
    client_id: str,
    web_host: str,
    timeout_seconds: float,
    transport: Any = None,
) -> DeviceCode:
    payload = await post_form(
        web_host,
        _DEVICE_PATH,
        {"client_id": client_id},
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    return parse_device_code(payload, web_host)


async def poll_for_token(
    *,
    client_id: str,
    web_host: str,
    device_code: str,
    interval: int,
    expires_in: int,
    timeout_seconds: float,
    transport: Any = None,
    sleep: SleepFn | None = None,
    mono: ClockFn | None = None,
    wall: ClockFn | None = None,
) -> GithubTokens:
    """Poll until the user approves, the code expires, or GitHub refuses.

    ``slow_down`` adds five seconds to the interval. The deadline is
    monotonic from ``expires_in``. After the last sleep that lands on
    the deadline, no further poll is sent.
    """
    sleeper = sleep or _asleep
    monotonic = mono or time.monotonic
    now = wall or time.time
    step = max(interval, 1)
    deadline = monotonic() + expires_in
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise GithubError("The code expired. Connect again.", phase="expired")
        await sleeper(min(float(step), remaining))
        if monotonic() >= deadline:
            raise GithubError("The code expired. Connect again.", phase="expired")
        payload = await post_form(
            web_host,
            _TOKEN_PATH,
            {
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": _DEVICE_GRANT,
            },
            timeout_seconds=timeout_seconds,
            transport=transport,
        )
        error = payload.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            step += 5
            continue
        if isinstance(error, str) and error:
            raise GithubError(public_oauth_message(error), phase=oauth_phase(error))
        return tokens_from_payload(payload, fallback_refresh="", now=now())


async def refresh_tokens(
    *,
    client_id: str,
    web_host: str,
    refresh_token: str,
    timeout_seconds: float,
    transport: Any = None,
    wall: ClockFn | None = None,
) -> GithubTokens:
    """Exchange a refresh token. GitHub rotates it; keep the new one when sent."""
    if not refresh_token:
        raise GithubError("GitHub sign-in expired. Connect again in Settings.")
    now = wall or time.time
    payload = await post_form(
        web_host,
        _TOKEN_PATH,
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    error = payload.get("error")
    if isinstance(error, str) and error:
        raise GithubError("GitHub sign-in expired. Connect again in Settings.")
    return tokens_from_payload(payload, fallback_refresh=refresh_token, now=now())
