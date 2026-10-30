"""Settings verbs for email (TD-3820, TD-3821, TD-4854).

Save writes ``notify.email`` and, when a password or a Google client
secret was typed, the keychain. An empty value leaves the stored secret
alone. Test sends one short sample report through the same newsletter
renderer a scheduled job uses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from ..config import EmailNotifyConfig, ModelConfig
from ..protocol import EmailTestResult, SetEmailNotify
from .email import EmailNotifyError, send
from .email_config import save_email_notify

# A miniature digest, so the owner sees the column, a table, and a link
# rather than a one-line plain-text ping.
_TEST_BODY = """\
# Sample report

This is a short sample of how a scheduled report will look.

- Headings, lists, and links stay in the column
- [An example](http://localhost/sample)

| Topic | Note |
| --- | --- |
| Delivery | Email |

> Sent so you can check the layout.
"""
_TEST_SUBJECT = "TST Desk test"


async def apply_email_notify(config: ModelConfig, msg: SetEmailNotify, path: Path) -> None:
    """Persist the form onto *config* and *path*. Raises ``EmailNotifyError``."""
    try:
        email = EmailNotifyConfig(
            enabled=msg.enabled,
            method=msg.method,
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
    # Same order as the SMTP password: a keychain refusal must not leave
    # the yaml (which has no secret field) ahead of a secret that did not land.
    if msg.google_client_secret:
        await _store_google_secret(msg.google_client_secret)
    # On the loop, with no await between its read and its write, like every
    # config.yaml writer: in a worker thread it could interleave with a
    # Settings → Configuration edit and drop it (TD-5203).
    save_email_notify(email, path)
    config.notify.email = email


async def send_test_email(config: ModelConfig, to: str) -> EmailTestResult:
    """One short sample report. The result is safe to put on the wire."""
    try:
        await send(
            config,
            _TEST_BODY,
            to=to,
            subject=_TEST_SUBJECT,
            title=_TEST_SUBJECT,
            when=datetime.now(UTC),
            preset=config.active_preset,
        )
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


async def _store_google_secret(secret: str) -> None:
    from ..keychain import KeychainError, store_google_client_secret

    try:
        await store_google_client_secret(secret)
    except KeychainError as exc:
        text = str(exc).replace(secret, "[redacted]")
        raise EmailNotifyError("KeychainError", text) from None
