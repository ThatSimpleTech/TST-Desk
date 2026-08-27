"""Discord incoming webhook (TD-4707).

Hermes shape: one ``send(config, message)``. Destination host comes from
``notify.discord.host`` in the user config. The webhook URL is a keychain
secret and is never written to yaml, logs, or the audit database.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from ..keychain import KeychainError, get_discord_webhook_url
from ..logging import get_logger
from ..protocol import ApprovalRequest, DaemonEvent, TurnComplete

if TYPE_CHECKING:
    from ..config import ModelConfig

log = get_logger("tstd.notify.discord")


def message_for(event: DaemonEvent) -> str | None:
    """Discord text for an approval or turn-complete event, or ``None``."""
    if isinstance(event, ApprovalRequest):
        body = event.summary or f"The agent wants to run {event.tool_name}"
        return f"Approval needed: {body}"
    if isinstance(event, TurnComplete):
        if event.failed:
            detail = event.error_code or "The turn did not finish"
            return f"Turn failed: {detail}"
        return "Turn complete: The agent finished a turn"
    return None


def _destination_url(config: ModelConfig, webhook_url: str) -> str | None:
    """Return *webhook_url* only when its host is the configured destination."""
    discord = config.notify.discord
    if not discord.enabled:
        return None
    host = discord.host.strip()
    if not host or not webhook_url.strip():
        return None
    parsed = urlsplit(webhook_url)
    if parsed.scheme not in ("http", "https"):
        log.warning("discord notify skipped: webhook URL is not http(s)")
        return None
    if (parsed.hostname or "").casefold() != host.casefold():
        log.warning("discord notify skipped: webhook host is not the configured destination")
        return None
    return webhook_url


async def send(
    config: ModelConfig,
    message: str,
    *,
    webhook_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """POST *message* to the Discord incoming webhook.

    No-op when Discord is off, the destination host is unset, or the
    keychain URL's host is not ``notify.discord.host``. Operational
    failures are logged without the URL and never raised.
    """
    if not config.notify.discord.enabled:
        return
    url = webhook_url
    if url is None:
        try:
            url = await get_discord_webhook_url()
        except KeychainError:
            log.warning("discord notify skipped: webhook missing from keychain")
            return
    dest = _destination_url(config, url)
    if dest is None:
        return
    timeout = config.notify.discord.timeout_seconds
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(dest, json={"content": message})
            response.raise_for_status()
    except httpx.HTTPError:
        # httpx error text often includes the request URL — do not log it.
        log.warning("discord notify failed")


async def _deliver(
    config: ModelConfig,
    message: str,
    webhook_url: str | None,
) -> None:
    try:
        await send(config, message, webhook_url=webhook_url)
    except Exception:
        # Never include the exception text — it can carry the webhook URL.
        log.warning("discord notify failed")


def schedule(
    config: ModelConfig,
    event: DaemonEvent,
    *,
    webhook_url: str | None = None,
) -> asyncio.Task[None] | None:
    """Fire-and-forget Discord delivery for approval or turn-complete.

    Returns the task so a caller can keep it alive; ``None`` when this
    event is not a notify or Discord is off.
    """
    if not config.notify.discord.enabled:
        return None
    text = message_for(event)
    if text is None:
        return None
    return asyncio.create_task(_deliver(config, text, webhook_url))
