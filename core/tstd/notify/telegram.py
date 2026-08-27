"""Telegram Bot API sendMessage (TD-4707).

Hermes shape: one ``send(config, message)``. Destination host comes from
``notify.telegram.host`` in the user config. The bot URL is a keychain
secret (``https://<host>/bot<token>/sendMessage?chat_id=<id>``) and is
never written to yaml, logs, or the audit database. ``chat_id`` lives
in that URL's query so the yaml shape stays ``enabled`` / ``host`` /
``timeout_seconds`` like Slack.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx

from ..keychain import KeychainError, get_telegram_bot_url
from ..logging import get_logger
from ..protocol import ApprovalRequest, DaemonEvent, TurnComplete

if TYPE_CHECKING:
    from ..config import ModelConfig

log = get_logger("tstd.notify.telegram")


def message_for(event: DaemonEvent) -> str | None:
    """Telegram text for an approval or turn-complete event, or ``None``."""
    if isinstance(event, ApprovalRequest):
        body = event.summary or f"The agent wants to run {event.tool_name}"
        return f"Approval needed: {body}"
    if isinstance(event, TurnComplete):
        if event.failed:
            detail = event.error_code or "The turn did not finish"
            return f"Turn failed: {detail}"
        return "Turn complete: The agent finished a turn"
    return None


def _destination(config: ModelConfig, bot_url: str) -> tuple[str, str] | None:
    """Return ``(post_url, chat_id)`` when the host matches config."""
    telegram = config.notify.telegram
    if not telegram.enabled:
        return None
    host = telegram.host.strip()
    if not host or not bot_url.strip():
        return None
    parsed = urlsplit(bot_url)
    if parsed.scheme not in ("http", "https"):
        log.warning("telegram notify skipped: bot URL is not http(s)")
        return None
    if (parsed.hostname or "").casefold() != host.casefold():
        log.warning("telegram notify skipped: bot host is not the configured destination")
        return None
    chat_ids = parse_qs(parsed.query).get("chat_id", [])
    chat_id = chat_ids[0].strip() if chat_ids else ""
    if not chat_id:
        log.warning("telegram notify skipped: chat_id missing from keychain URL")
        return None
    dest = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return dest, chat_id


async def send(
    config: ModelConfig,
    message: str,
    *,
    bot_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """POST *message* to Telegram ``sendMessage``.

    No-op when Telegram is off, the destination host is unset, the
    keychain URL's host is not ``notify.telegram.host``, or ``chat_id``
    is missing. Operational failures are logged without the URL and
    never raised.
    """
    if not config.notify.telegram.enabled:
        return
    url = bot_url
    if url is None:
        try:
            url = await get_telegram_bot_url()
        except KeychainError:
            log.warning("telegram notify skipped: bot URL missing from keychain")
            return
    dest = _destination(config, url)
    if dest is None:
        return
    post_url, chat_id = dest
    timeout = config.notify.telegram.timeout_seconds
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(post_url, json={"chat_id": chat_id, "text": message})
            response.raise_for_status()
    except httpx.HTTPError:
        # httpx error text often includes the request URL — do not log it.
        log.warning("telegram notify failed")


async def _deliver(
    config: ModelConfig,
    message: str,
    bot_url: str | None,
) -> None:
    try:
        await send(config, message, bot_url=bot_url)
    except Exception:
        # Never include the exception text — it can carry the bot token.
        log.warning("telegram notify failed")


def schedule(
    config: ModelConfig,
    event: DaemonEvent,
    *,
    bot_url: str | None = None,
) -> asyncio.Task[None] | None:
    """Fire-and-forget Telegram delivery for approval or turn-complete.

    Returns the task so a caller can keep it alive; ``None`` when this
    event is not a notify or Telegram is off.
    """
    if not config.notify.telegram.enabled:
        return None
    text = message_for(event)
    if text is None:
        return None
    return asyncio.create_task(_deliver(config, text, bot_url))
