"""SMTP delivery for a scheduled job's report (TD-3820).

Same shape as the other notifiers: ``send(config, message)``. The only
host it may reach is ``notify.email.host``. The password is a keychain
secret (account ``tst-smtp-password``) and is never written to config,
logs, or the audit database. The message body is not logged either —
only the recipient's domain, so a report cannot land in a log file.

STARTTLS (port 587) and implicit TLS (port 465) are the only modes.
A server that will not negotiate TLS is refused before AUTH.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import re
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..logging import get_logger

if TYPE_CHECKING:
    from ..config import ModelConfig

log = get_logger("tstd.notify.email")

_ADDRESS = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET = re.compile(r"^[-*]\s+(.*)$")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+|mailto:[^\s)]+)\)")
_PUBLIC_LIMIT = 300


class EmailNotifyError(Exception):
    """A send that did not happen. ``message`` is safe to show and store."""

    def __init__(self, error_class: str, message: str) -> None:
        self.error_class = error_class
        self.message = message
        super().__init__(f"{error_class}: {message}")


def email_subject(instruction: str, when: datetime, timezone_name: str | None) -> str:
    """``<first 60 chars of the instruction> — <local date>``.

    Jobs have no display name. The date is the job's zone when it has
    one, otherwise the machine's local zone.
    """
    label = " ".join(instruction.split())
    label = label[:60] if label else "Scheduled job"
    local = _local_when(when, timezone_name)
    return f"{label} — {local.date().isoformat()}"


def _local_when(when: datetime, timezone_name: str | None) -> datetime:
    if timezone_name:
        try:
            return when.astimezone(ZoneInfo(timezone_name))
        except (KeyError, ValueError, OSError):
            pass
    return when.astimezone()


def render_minimal_html(markdown: str) -> str:
    """Headings, lists, and links. No dependency, so no other Markdown."""
    blocks: list[str] = []
    items: list[str] = []

    def close_list() -> None:
        if items:
            blocks.append("<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>")
            items.clear()

    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            close_list()
            continue
        heading = _HEADING.match(line)
        if heading is not None:
            close_list()
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue
        bullet = _BULLET.match(line)
        if bullet is not None:
            items.append(_inline(bullet.group(1)))
            continue
        close_list()
        blocks.append(f"<p>{_inline(line)}</p>")
    close_list()
    body = "".join(blocks) if blocks else f"<p>{_inline(markdown)}</p>"
    return f"<html><body>{body}</body></html>"


def _inline(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in _LINK.finditer(text):
        parts.append(html.escape(text[cursor : match.start()]))
        label = html.escape(match.group(1))
        href = html.escape(match.group(2), quote=True)
        parts.append(f'<a href="{href}">{label}</a>')
        cursor = match.end()
    parts.append(html.escape(text[cursor:]))
    return "".join(parts)


async def send(
    config: ModelConfig,
    message: str,
    *,
    to: str,
    subject: str,
    ssl_context: ssl.SSLContext | None = None,
) -> None:
    """Send *message* to one address. Raises ``EmailNotifyError`` on failure.

    ``ssl_context`` is a test seam. Production uses the default context,
    which verifies the server certificate.
    """
    email_cfg = config.notify.email
    _require_ready(
        email_cfg.enabled, email_cfg.host, email_cfg.username, email_cfg.from_address, to
    )
    password = await _load_password()
    if not password:
        raise EmailNotifyError("KeychainError", "SMTP password is not in the keychain")
    domain = _domain(to)
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
        client.send_message(_message(from_address, to, subject, body))
        client.quit()
        client = None
    except EmailNotifyError:
        raise
    except Exception as exc:
        error_class, text = _public_error(exc, password, body)
        raise EmailNotifyError(error_class, text) from None
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


def _require_tls(client: smtplib.SMTP) -> None:
    """Refuse AUTH until the socket is actually TLS. A plaintext banner is not enough."""
    if not isinstance(client.sock, ssl.SSLSocket):
        raise EmailNotifyError("SMTPException", "TLS was not established")


def _message(from_address: str, to: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = from_address
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    message.add_alternative(render_minimal_html(body), subtype="html")
    return message


def _public_error(exc: BaseException, password: str, body: str) -> tuple[str, str]:
    """SMTP text can echo the AUTH secret or the report. Neither leaves this function."""
    text = str(exc)
    if password:
        text = text.replace(password, "[redacted]")
    if body:
        text = text.replace(body, "[redacted]")
    text = " ".join(text.split())
    if len(text) > _PUBLIC_LIMIT:
        text = text[:_PUBLIC_LIMIT]
    return type(exc).__name__, text or "send failed"
