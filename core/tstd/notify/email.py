"""SMTP delivery for a scheduled job's report (TD-3820, TD-3821).

Same shape as the other notifiers: ``send(config, message)``. The only
host it may reach is ``notify.email.host``. The password is a keychain
secret (account ``tst-smtp-password``) and is never written to config,
logs, or the audit database. The message body is not logged either —
only the recipient's domain, so a report cannot land in a log file.

The message is multipart: the report Markdown as plain text, and the
same Markdown as a newsletter. The model text is untrusted; the HTML
renderer escapes it.

STARTTLS (port 587) and implicit TLS (port 465) are the only modes.
A server that will not negotiate TLS is refused before AUTH.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import TYPE_CHECKING

from ..logging import get_logger
from .email_html import local_when, render_report_html

if TYPE_CHECKING:
    from ..config import ModelConfig

log = get_logger("tstd.notify.email")

_ADDRESS = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_PUBLIC_LIMIT = 300


class EmailNotifyError(Exception):
    """A send that did not happen. ``message`` is safe to show and store."""

    def __init__(self, error_class: str, message: str) -> None:
        self.error_class = error_class
        self.message = message
        super().__init__(f"{error_class}: {message}")


@dataclass(frozen=True)
class ReportChrome:
    """Subject line plus the header and footer of one report."""

    subject: str
    title: str
    when: datetime
    timezone_name: str | None
    preset: str


def job_title(instruction: str) -> str:
    """First 60 characters of the instruction. Jobs have no display name."""
    label = " ".join(instruction.split())
    return label[:60] if label else "Scheduled job"


def email_subject(instruction: str, when: datetime, timezone_name: str | None) -> str:
    """``<first 60 chars of the instruction> — <local date>``.

    The date is the job's zone when it has one, otherwise the machine's
    local zone.
    """
    local = local_when(when, timezone_name)
    return f"{job_title(instruction)} — {local.date().isoformat()}"


def report_chrome(
    instruction: str,
    when: datetime,
    timezone_name: str | None,
    preset: str | None,
    active_preset: str,
) -> ReportChrome:
    """Header and footer for one fire.

    A job with no pin ran on the window's active preset. The footer
    names that preset.
    """
    chosen = preset.strip() if preset and preset.strip() else active_preset
    return ReportChrome(
        subject=email_subject(instruction, when, timezone_name),
        title=job_title(instruction),
        when=when,
        timezone_name=timezone_name,
        preset=chosen,
    )


async def send(
    config: ModelConfig,
    message: str,
    *,
    to: str,
    subject: str,
    ssl_context: ssl.SSLContext | None = None,
    title: str | None = None,
    when: datetime | None = None,
    timezone_name: str | None = None,
    preset: str | None = None,
) -> None:
    """Send *message* to one address. Raises ``EmailNotifyError`` on failure.

    ``ssl_context`` is a test seam. Production uses the default context,
    which verifies the server certificate. The HTML part uses *title*,
    *when*, and *preset*; omitted values fall back to a generic label,
    the current time, and the active preset.
    """
    email_cfg = config.notify.email
    _require_ready(
        email_cfg.enabled, email_cfg.host, email_cfg.username, email_cfg.from_address, to
    )
    password = await _load_password()
    if not password:
        raise EmailNotifyError("KeychainError", "SMTP password is not in the keychain")
    domain = _domain(to)
    html_body = render_report_html(
        message,
        title=title.strip() if title and title.strip() else "Scheduled job",
        when=when if when is not None else datetime.now(UTC),
        timezone_name=timezone_name,
        preset=preset.strip() if preset and preset.strip() else config.active_preset,
    )
    try:
        await asyncio.to_thread(
            _smtp_send,
            host=email_cfg.host.strip(),
            port=email_cfg.port,
            security=email_cfg.security,
            username=email_cfg.username.strip(),
            from_address=email_cfg.from_address.strip(),
            to=to.strip(),
            subject=subject,
            body=message,
            html_body=html_body,
            password=password,
            timeout=email_cfg.timeout_seconds,
            ssl_context=ssl_context,
        )
    except EmailNotifyError as exc:
        log.warning(
            "email notify failed",
            extra={"extra_fields": {"error_class": exc.error_class, "domain": domain}},
        )
        raise
    log.info("email notify sent", extra={"extra_fields": {"domain": domain}})


def _require_ready(enabled: bool, host: str, username: str, from_address: str, to: str) -> None:
    if not enabled:
        raise EmailNotifyError("EmailNotifyError", "email notify is disabled")
    cleaned = host.strip()
    if not cleaned or any(char.isspace() or char == "/" for char in cleaned):
        raise EmailNotifyError("EmailNotifyError", "notify.email.host is not a hostname")
    if not username.strip():
        raise EmailNotifyError("EmailNotifyError", "notify.email.username is empty")
    if not _ADDRESS.fullmatch(from_address.strip()):
        raise EmailNotifyError("EmailNotifyError", "notify.email.from_address is not one address")
    if not _ADDRESS.fullmatch(to.strip()):
        raise EmailNotifyError("EmailNotifyError", "recipient is not one email address")


async def _load_password() -> str:
    """Function-local import so the test keychain seam covers this call."""
    from ..keychain import KeychainError, get_smtp_password

    try:
        return await get_smtp_password()
    except KeychainError:
        return ""


def _domain(address: str) -> str:
    if "@" not in address:
        return ""
    return address.rsplit("@", 1)[-1].strip().lower()


def _smtp_send(
    *,
    host: str,
    port: int,
    security: str,
    username: str,
    from_address: str,
    to: str,
    subject: str,
    body: str,
    html_body: str,
    password: str,
    timeout: float,
    ssl_context: ssl.SSLContext | None,
) -> None:
    context = ssl_context if ssl_context is not None else ssl.create_default_context()
    client: smtplib.SMTP | None = None
    try:
        if security == "tls":
            client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
            _require_tls(client)
            client.ehlo()
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
            client.ehlo()
            if not client.has_extn("starttls"):
                raise EmailNotifyError("SMTPException", "server did not offer STARTTLS")
            client.starttls(context=context)
            client.ehlo()
            _require_tls(client)
        client.login(username, password)
        client.send_message(_message(from_address, to, subject, body, html_body))
        client.quit()
        client = None
    except EmailNotifyError:
        raise
    except Exception as exc:
        error_class, text = _public_error(exc, password, body, html_body)
        raise EmailNotifyError(error_class, text) from None
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


def _require_tls(client: smtplib.SMTP) -> None:
    """Refuse AUTH until the socket is actually TLS. A plaintext banner is not enough."""
    if not isinstance(client.sock, ssl.SSLSocket):
        raise EmailNotifyError("SMTPException", "TLS was not established")


def _message(from_address: str, to: str, subject: str, body: str, html_body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = from_address
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    message.add_alternative(html_body, subtype="html")
    return message


def _public_error(exc: BaseException, password: str, body: str, html_body: str) -> tuple[str, str]:
    """SMTP text can echo the AUTH secret or the report. Neither leaves this function."""
    text = str(exc)
    if password:
        text = text.replace(password, "[redacted]")
    # The HTML document is the longer form of the same report. Replace it
    # before the plain body so a quoted copy of the whole message goes first.
    if html_body:
        text = text.replace(html_body, "[redacted]")
    if body:
        text = text.replace(body, "[redacted]")
    text = " ".join(text.split())
    if len(text) > _PUBLIC_LIMIT:
        text = text[:_PUBLIC_LIMIT]
    return type(exc).__name__, text or "send failed"
