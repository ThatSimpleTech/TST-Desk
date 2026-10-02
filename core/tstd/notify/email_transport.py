"""Choose SMTP or Microsoft Graph for one rendered report (TD-4853).

``notify.email.method`` is the switch. Both paths send the same plain
text and the same HTML newsletter.
"""

from __future__ import annotations

import ssl
from typing import Any

from ..config import ModelConfig


async def deliver_report(
    config: ModelConfig,
    *,
    to: str,
    subject: str,
    body: str,
    html_body: str,
    ssl_context: ssl.SSLContext | None = None,
    token_store: Any = None,
    transport: Any = None,
) -> None:
    """Send *body* and *html_body*. Raises ``EmailNotifyError``."""
    if config.notify.email.method == "microsoft":
        from ..connectors.microsoft_mail import send_graph
        from ..connectors.microsoft_models import MicrosoftError
        from .email import EmailNotifyError, require_graph_ready

        require_graph_ready(config.notify.email.enabled, to)
        try:
            await send_graph(
                config,
                to=to,
                subject=subject,
                body=body,
                html_body=html_body,
                from_address=config.notify.email.from_address,
                store=token_store,
                transport=transport,
            )
        except MicrosoftError as exc:
            raise EmailNotifyError("MicrosoftError", exc.message) from None
        return
    from .email import smtp_deliver

    await smtp_deliver(
        config,
        to=to,
        subject=subject,
        body=body,
        html_body=html_body,
        ssl_context=ssl_context,
    )
