"""ntfy topic POST (TD-3802).

Hermes shape: one ``send(config, message)``. Destination host comes from
``notify.ntfy.host`` in the user config. The topic URL is a keychain
secret and is never written to yaml, logs, or the audit database.
Discord and Telegram are TD-4707, not this module.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from ..keychain import KeychainError, get_ntfy_topic_url
from ..logging import get_logger
from ..protocol import ApprovalRequest, DaemonEvent, TurnComplete

if TYPE_CHECKING:
    from ..config import ModelConfig

log = get_logger("tstd.notify.ntfy")


def message_for(event: DaemonEvent) -> str | None:
    """ntfy text for an approval or turn-complete event, or ``None``."""
    if isinstance(event, ApprovalRequest):
        body = event.summary or f"The agent wants to run {event.tool_name}"
        return f"Approval needed: {body}"
    if isinstance(event, TurnComplete):
        if event.failed:
            detail = event.error_code or "The turn did not finish"
            return f"Turn failed: {detail}"
        return "Turn complete: The agent finished a turn"
    return None


def _destination_url(config: ModelConfig, topic_url: str) -> str | None:
    """Return *topic_url* only when its host is the configured destination."""
    ntfy = config.notify.ntfy
    if not ntfy.enabled:
        return None
    host = ntfy.host.strip()
    if not host or not topic_url.strip():
        return None
    parsed = urlsplit(topic_url)
    if parsed.scheme not in ("http", "https"):
        log.warning("ntfy notify skipped: topic URL is not http(s)")
        return None
    if (parsed.hostname or "").casefold() != host.casefold():
        log.warning("ntfy notify skipped: topic host is not the configured destination")
        return None
    return topic_url


async def send(
    config: ModelConfig,
    message: str,
    *,
    topic_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """POST *message* to the ntfy topic URL.

    No-op when ntfy is off, the destination host is unset, or the
    keychain URL's host is not ``notify.ntfy.host``. Operational
    failures are logged without the URL and never raised.
    """
    if not config.notify.ntfy.enabled:
        return
    url = topic_url
    if url is None:
        try:
            url = await get_ntfy_topic_url()
        except KeychainError:
            log.warning("ntfy notify skipped: topic missing from keychain")
            return
    dest = _destination_url(config, url)
    if dest is None:
        return
    timeout = config.notify.ntfy.timeout_seconds
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(dest, content=message)
            response.raise_for_status()
    except httpx.HTTPError:
        # httpx error text often includes the request URL — do not log it.
        log.warning("ntfy notify failed")


async def _deliver(
    config: ModelConfig,
    message: str,
    topic_url: str | None,
) -> None:
    try:
        await send(config, message, topic_url=topic_url)
    except Exception:
        # Never include the exception text — it can carry the topic URL.
        log.warning("ntfy notify failed")


def schedule(
    config: ModelConfig,
    event: DaemonEvent,
    *,
    topic_url: str | None = None,
) -> asyncio.Task[None] | None:
    """Fire-and-forget ntfy delivery for approval or turn-complete.

    Returns the task so a caller can keep it alive; ``None`` when this
    event is not a notify or ntfy is off.
    """
    if not config.notify.ntfy.enabled:
        return None
    text = message_for(event)
    if text is None:
        return None
    return asyncio.create_task(_deliver(config, text, topic_url))
