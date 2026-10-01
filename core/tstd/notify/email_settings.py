"""Settings verbs for SMTP (TD-3820).

Save writes ``notify.email`` and, when a password was typed, the
keychain. An empty password leaves the stored secret alone. Test sends
one short message to a typed address and reports the SMTP class.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import ValidationError

from ..config import EmailNotifyConfig, ModelConfig
from ..protocol import EmailTestResult, SetEmailNotify
from .email import EmailNotifyError, send
from .email_config import save_email_notify

_TEST_BODY = "This is a test email from TST Desk."
_TEST_SUBJECT = "TST Desk test"


async def apply_email_notify(config: ModelConfig, msg: SetEmailNotify, path: Path) -> None:
    """Persist the form onto *config* and *path*. Raises ``EmailNotifyError``."""
    try:
        email = EmailNotifyConfig(
            enabled=msg.enabled,
            host=msg.host,
            port=msg.port,
            security=msg.security,
            username=msg.username,
            from_address=msg.from_address,
            timeout_seconds=config.notify.email.timeout_seconds,
        )
    except ValidationError:
        raise EmailNotifyError("ValidationError", "email settings are invalid") from None
    # Store first. A keychain refusal must not leave the yaml ahead of
    # the in-memory config the settings form is about to re-read.
    if msg.password:
        await _store_password(msg.password)
    await asyncio.to_thread(save_email_notify, email, path)
    config.notify.email = email


async def send_test_email(config: ModelConfig, to: str) -> EmailTestResult:
    """One short message. The result is safe to put on the wire."""
    try:
        await send(config, _TEST_BODY, to=to, subject=_TEST_SUBJECT)
    except EmailNotifyError as exc:
        return EmailTestResult(ok=False, error_class=exc.error_class, message=exc.message)
    return EmailTestResult(ok=True, message="Sent")


async def _store_password(password: str) -> None:
    from ..keychain import KeychainError, store_smtp_password

    try:
        await store_smtp_password(password)
    except KeychainError as exc:
        text = str(exc).replace(password, "[redacted]")
        raise EmailNotifyError("KeychainError", text) from None
