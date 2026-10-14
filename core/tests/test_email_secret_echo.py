"""The SMTP password stays out of error text in every form it takes (TD-3820).

A server reply can quote the base64 AUTH token, which the shared
redactor does not see as a secret. A malformed ``set_email_notify``
would otherwise have its input quoted back in the validation error.
"""

from __future__ import annotations

import base64
import json
import logging
import smtplib
from pathlib import Path

import pytest

from tests.test_email_notify import (
    _BODY,
    _PASSWORD,
    FakeSmtp,
    _cert,
    _client_ctx,
    _email_config,
    _logged,
    _server_ctx,
)
from tests.test_loop import make_config
from tstd.notify.email import EmailNotifyError, _public_error, _secret_forms, send
from tstd.protocol import HandshakeError, parse_client_message

_WRONG = "wrong-password-77"


def _b64(raw: str) -> str:
    return base64.b64encode(raw.encode()).decode("ascii")


async def test_a_quoted_auth_plain_token_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _get() -> str:
        return _WRONG

    monkeypatch.setattr("tstd.keychain.get_smtp_password", _get)
    cert, key = _cert(tmp_path)
    server = FakeSmtp(
        advertise_starttls=True,
        implicit_tls=False,
        ctx=_server_ctx(cert, key),
        echo_token=True,
    )
    await server.start()
    try:
        config = make_config()
        config.notify.email = _email_config(server.port)
        with (
            caplog.at_level(logging.DEBUG, logger="tstd.notify.email"),
            pytest.raises(EmailNotifyError) as caught,
        ):
            await send(
                config, _BODY, to="owner@example.com", subject="t", ssl_context=_client_ctx()
            )
    finally:
        await server.close()
    token = _b64(f"\0desk\0{_WRONG}")
    # The client sent this exact token, and the fake's 535 quoted it back.
    assert any(token in command for command in server.commands)
    message = str(caught.value)
    assert "[redacted]" in message
    for leaked in (token, _WRONG, _b64(_WRONG), _PASSWORD):
        assert leaked not in message
        assert leaked not in _logged(caplog)


def test_a_quoted_auth_login_password_is_redacted() -> None:
    """AUTH LOGIN sends the password alone, base64. A 535 may quote that line."""
    quoted = smtplib.SMTPAuthenticationError(535, f"rejected {_b64(_WRONG)}".encode())
    error_class, text = _public_error(quoted, _secret_forms("desk", _WRONG), "", "")
    assert error_class == "SMTPAuthenticationError"
    assert _b64(_WRONG) not in text
    assert "[redacted]" in text


def test_no_password_means_nothing_to_redact() -> None:
    assert _secret_forms("desk", "") == []


@pytest.mark.parametrize(
    "fields",
    [
        {"password": [_WRONG]},
        {"password": {"value": _WRONG}},
        {"password": _WRONG, "port": "not-a-port", "security": _WRONG},
    ],
)
def test_a_malformed_set_email_notify_does_not_quote_its_input(fields: dict[str, object]) -> None:
    raw = {"type": "set_email_notify", "host": "smtp.example.com", **fields}
    with pytest.raises(HandshakeError) as caught:
        parse_client_message(json.dumps(raw))
    assert caught.value.code == "bad_request"
    assert _WRONG not in caught.value.message
    assert "set_email_notify" in caught.value.message
