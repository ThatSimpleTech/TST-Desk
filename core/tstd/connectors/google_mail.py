"""Send one report through the Gmail API (TD-4854).

``users.messages.send`` takes the RFC 2822 message, base64url-encoded,
in a JSON ``raw`` field. The message is the same multipart report SMTP
already builds (TD-3821). The token stays in the header. The response
body is not read: it can echo the message.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from ..config import GoogleConnectorConfig, ModelConfig
from .google_flow import fetch_account
from .google_http import post_json
from .google_models import NO_MAILBOX, OFF_MESSAGE, GoogleError
from .google_tokens import (
    KeychainSecretStore,
    KeychainTokenStore,
    SecretStore,
    TokenStore,
    ensure_access,
)

_SEND_PATH = "/gmail/v1/users/me/messages/send"
_ADDRESS = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def encode_mime(raw: bytes) -> str:
    """Unpadded base64url. That is the ``raw`` field of ``users.messages.send``."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


async def send_gmail(
    config: ModelConfig,
    *,
    to: str,
    subject: str,
    body: str,
    html_body: str,
    from_address: str,
    store: TokenStore | None = None,
    secrets: SecretStore | None = None,
    transport: Any = None,
) -> None:
    """POST the report. Raises ``GoogleError`` with a message safe to show."""
    cfg = config.connectors.google
    if not cfg.enabled():
        raise GoogleError(OFF_MESSAGE, phase="off")
    holder = store if store is not None else KeychainTokenStore()
    secret_store = secrets if secrets is not None else KeychainSecretStore()
    token = await ensure_access(cfg, holder, secret_store, transport=transport)
    sender = await _sender(cfg, token, from_address, transport)
    encoded = _raw(sender, to, subject, body, html_body)
    status = await _post(cfg, encoded, token, transport)
    if status == 401:
        token = await ensure_access(cfg, holder, secret_store, transport=transport, force=True)
        sender = await _sender(cfg, token, from_address, transport)
        encoded = _raw(sender, to, subject, body, html_body)
        status = await _post(cfg, encoded, token, transport)
    if status != 200:
        raise GoogleError(f"Gmail rejected the message (HTTP {status}).")


def _raw(sender: str, to: str, subject: str, body: str, html_body: str) -> bytes:
    # Imported here: notify.email imports this module from inside send().
    from ..notify.email import report_message

    message = report_message(sender, to.strip(), subject, body, html_body)
    payload = json.dumps({"raw": encode_mime(message.as_bytes())}, separators=(",", ":"))
    return payload.encode("ascii")


async def _sender(
    cfg: GoogleConnectorConfig,
    token: str,
    from_address: str,
    transport: Any,
) -> str:
    cleaned = from_address.strip()
    if _ADDRESS.fullmatch(cleaned):
        return cleaned
    account = await fetch_account(cfg, token, transport=transport)
    if account:
        return account
    raise GoogleError(NO_MAILBOX)


async def _post(
    cfg: GoogleConnectorConfig,
    encoded: bytes,
    token: str,
    transport: Any,
) -> int:
    return await post_json(
        cfg.gmail_host,
        _SEND_PATH,
        encoded,
        token,
        timeout_seconds=cfg.timeout_seconds,
        transport=transport,
    )
