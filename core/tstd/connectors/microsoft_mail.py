"""Send one report through Microsoft Graph (TD-4853).

Graph's JSON ``sendMail`` body is a single part. The scheduled report is
HTML plus the same Markdown as text (TD-3821), so this posts that
multipart message as base64 to ``/v1.0/me/sendMail``. The token stays in
the header. The response body is not read: it can echo the message.
"""

from __future__ import annotations

import base64
import re
from typing import Any

from ..config import MicrosoftConnectorConfig, ModelConfig
from .microsoft_flow import fetch_account
from .microsoft_http import post_text
from .microsoft_models import NO_MAILBOX, OFF_MESSAGE, MicrosoftError
from .microsoft_tokens import KeychainTokenStore, TokenStore, ensure_access

_SEND_PATH = "/v1.0/me/sendMail"
_ADDRESS = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


async def send_graph(
    config: ModelConfig,
    *,
    to: str,
    subject: str,
    body: str,
    html_body: str,
    from_address: str,
    store: TokenStore | None = None,
    transport: Any = None,
) -> None:
    """POST the report. Raises ``MicrosoftError`` with a message safe to show."""
    cfg = config.connectors.microsoft
    if not cfg.enabled():
        raise MicrosoftError(OFF_MESSAGE, phase="off")
    holder = store if store is not None else KeychainTokenStore()
    token = await ensure_access(cfg, holder, transport=transport)
    sender = await _sender(cfg, token, from_address, transport)
    # Imported here: notify.email imports this module from inside send().
    from ..notify.email import report_message

    message = report_message(sender, to.strip(), subject, body, html_body)
    encoded = base64.b64encode(message.as_bytes())
    status = await _post(cfg, encoded, token, transport)
    if status == 401:
        token = await ensure_access(cfg, holder, transport=transport, force=True)
        sender = await _sender(cfg, token, from_address, transport)
        message = report_message(sender, to.strip(), subject, body, html_body)
        encoded = base64.b64encode(message.as_bytes())
        status = await _post(cfg, encoded, token, transport)
    if status not in {200, 202}:
        raise MicrosoftError(f"Microsoft Graph rejected the message (HTTP {status}).")


async def _sender(
    cfg: MicrosoftConnectorConfig,
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
    raise MicrosoftError(NO_MAILBOX)


async def _post(
    cfg: MicrosoftConnectorConfig,
    encoded: bytes,
    token: str,
    transport: Any,
) -> int:
    return await post_text(
        cfg.graph_host,
        _SEND_PATH,
        encoded,
        token,
        timeout_seconds=cfg.timeout_seconds,
        transport=transport,
    )
