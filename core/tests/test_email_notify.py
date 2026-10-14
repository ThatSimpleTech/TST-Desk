"""SMTP delivery for a scheduled report (TD-3820).

The server is an in-process fake bound to 127.0.0.1. Nothing leaves the
machine, and the password comes from the keychain seam, never a real
keychain.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import ssl
import subprocess
from datetime import UTC, datetime
from email import message_from_bytes
from email.policy import default as email_default
from pathlib import Path

import pytest

from tests.test_loop import make_config
from tstd.config import EmailNotifyConfig
from tstd.keychain import KeychainError
from tstd.notify.email import EmailNotifyError, email_subject, send
from tstd.notify.email_config import save_email_notify
from tstd.notify.email_html import render_report_html
from tstd.notify.email_settings import apply_email_notify
from tstd.protocol import AssistantDelta, SetEmailNotify, ToolCall
from tstd.scheduler.email_delivery import final_assistant_text

_PASSWORD = "s3cret-smtp-pw"
_BODY = "FINAL-REPORT-TOKEN\n\n# Findings\n\n- one\n- [notes](https://example.com/n)"


def _cert(tmp_path: Path) -> tuple[Path, Path]:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def _server_ctx(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


def _client_ctx() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class FakeSmtp:
    """Enough SMTP to prove STARTTLS, AUTH, and the message. Loopback only."""

    def __init__(
        self, *, advertise_starttls: bool, implicit_tls: bool, ctx: ssl.SSLContext
    ) -> None:
        self.advertise_starttls = advertise_starttls
        self.implicit_tls = implicit_tls
        self.ctx = ctx
        self.commands: list[str] = []
        self.data = b""
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        ssl_ctx = self.ctx if self.implicit_tls else None
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0, ssl=ssl_ctx)
        sock = self._server.sockets[0]
        host, port = sock.getsockname()[:2]
        assert host == "127.0.0.1"
        self.port = port

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tls = self.implicit_tls
        try:
            writer.write(b"220 localhost ESMTP\r\n")
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                upper = text.upper()
                # Keep the original line. AUTH PLAIN's token is base64, and
                # uppercasing it would hide the password the test decodes.
                self.commands.append(text)
                if upper.startswith("EHLO") or upper.startswith("HELO"):
                    parts = ["250-localhost"]
                    if self.advertise_starttls and not tls:
                        parts.append("250-STARTTLS")
                    parts.append("250 AUTH PLAIN")
                    writer.write(("\r\n".join(parts) + "\r\n").encode())
                elif upper == "STARTTLS":
                    writer.write(b"220 Ready\r\n")
                    await writer.drain()
                    await writer.start_tls(self.ctx, ssl_handshake_timeout=5)
                    tls = True
                    continue
                elif upper.startswith("AUTH"):
                    supplied = _plain_password(text)
                    if supplied != _PASSWORD:
                        writer.write(f"535 rejected {supplied}\r\n".encode())
                    else:
                        writer.write(b"235 OK\r\n")
                elif upper == "DATA":
                    writer.write(b"354 Go\r\n")
                    await writer.drain()
                    buf = bytearray()
                    while True:
                        chunk = await reader.readline()
                        if chunk in (b".\r\n", b".\n") or chunk == b"":
                            break
                        if chunk.startswith(b".."):
                            chunk = chunk[1:]
                        buf.extend(chunk)
                    self.data = bytes(buf)
                    writer.write(b"250 OK\r\n")
                elif upper == "QUIT":
                    writer.write(b"221 Bye\r\n")
                    await writer.drain()
                    break
                else:
                    writer.write(b"250 OK\r\n")
                await writer.drain()
        except Exception:
            return
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                return


def _plain_password(command: str) -> str:
    token = command.split(" ", 2)[-1]
    try:
        raw = base64.b64decode(token)
    except ValueError:
        return ""
    parts = raw.split(b"\0")
    if len(parts) < 3:
        return ""
    return parts[2].decode("utf-8", "replace")


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    chunks = [caplog.text]
    for record in caplog.records:
        chunks.append(record.getMessage())
        chunks.append(str(getattr(record, "extra_fields", "")))
    return "\n".join(chunks)


def _email_config(
    port: int, *, security: str = "starttls", enabled: bool = True
) -> EmailNotifyConfig:
    return EmailNotifyConfig(
        enabled=enabled,
        host="127.0.0.1",
        port=port,
        security=security,  # type: ignore[arg-type]
        username="desk",
        from_address="desk@example.com",
        timeout_seconds=5,
    )


@pytest.fixture
def password(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _get() -> str:
        return _PASSWORD

    monkeypatch.setattr("tstd.keychain.get_smtp_password", _get)


def _parsed(server: FakeSmtp):
    return message_from_bytes(server.data, policy=email_default)


class TestSubjectAndReport:
    @pytest.mark.parametrize(
        ("instruction", "zone", "expected"),
        [
            ("Morning brief", "America/Chicago", "Morning brief — 2026-10-01"),
            ("", "America/Chicago", "Scheduled job — 2026-10-01"),
            ("  daily   research  ", None, None),
        ],
    )
    def test_subject(self, instruction: str, zone: str | None, expected: str | None) -> None:
        when = datetime(2026, 10, 2, 4, 0, tzinfo=UTC)
        subject = email_subject(instruction, when, zone)
        if expected is None:
            assert subject.endswith(f"— {when.astimezone().date().isoformat()}")
            assert subject.startswith("daily research")
            return
        assert subject == expected

    def test_long_instruction_stops_at_60(self) -> None:
        when = datetime(2026, 10, 2, 4, 0, tzinfo=UTC)
        subject = email_subject("a" * 80, when, "UTC")
        label, _, date = subject.partition(" — ")
        assert len(label) == 60
        assert date == "2026-10-02"

    def test_final_assistant_text_is_the_suffix_after_the_last_tool(self) -> None:
        events = [
            AssistantDelta(session_id="s", seq=1, delta="let me search"),
            ToolCall(session_id="s", seq=2, tool_call_id="t", name="web_search", arguments={}),
            AssistantDelta(session_id="s", seq=3, delta="FINAL-REPORT-TOKEN"),
        ]
        assert final_assistant_text(events) == "FINAL-REPORT-TOKEN"

    def test_no_tool_call_uses_the_whole_turn(self) -> None:
        events = [
            AssistantDelta(session_id="s", seq=1, delta="hello "),
            AssistantDelta(session_id="s", seq=2, delta="there"),
        ]
        assert final_assistant_text(events) == "hello there"

    def test_empty_suffix_falls_back_to_the_full_text(self) -> None:
        events = [
            AssistantDelta(session_id="s", seq=1, delta="only narration"),
            ToolCall(session_id="s", seq=2, tool_call_id="t", name="web_search", arguments={}),
        ]
        assert final_assistant_text(events) == "only narration"

    def test_html_renders_heading_list_and_link(self) -> None:
        # The newsletter puts inline styles on the tags. The old exact
        # `<h1>Findings</h1>` string was the minimal renderer this replaced.
        page = render_report_html(
            _BODY,
            title="Morning brief",
            when=datetime(2026, 10, 2, 4, 0, tzinfo=UTC),
            timezone_name="America/Chicago",
            preset="grok",
        )
        assert "<h1" in page and "Findings" in page
        assert "<li" in page and ">one</li>" in page
        assert 'href="https://example.com/n"' in page
        assert "FINAL-REPORT-TOKEN" in page


class TestSmtp:
    async def test_plaintext_is_refused(self, tmp_path: Path, password: None) -> None:
        cert, key = _cert(tmp_path)
        server = FakeSmtp(advertise_starttls=False, implicit_tls=False, ctx=_server_ctx(cert, key))
        await server.start()
        try:
            config = make_config()
            config.notify.email = _email_config(server.port)
            with pytest.raises(EmailNotifyError, match="STARTTLS"):
                await send(
                    config, _BODY, to="owner@example.com", subject="Morning brief — 2026-10-01"
                )
        finally:
            await server.close()
        assert not any(
            cmd.upper().startswith("AUTH") or cmd.upper() == "DATA" for cmd in server.commands
        )

    async def test_starttls_auths_with_the_keychain_password(
        self, tmp_path: Path, password: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        cert, key = _cert(tmp_path)
        server = FakeSmtp(advertise_starttls=True, implicit_tls=False, ctx=_server_ctx(cert, key))
        await server.start()
        when = datetime(2026, 10, 2, 4, 0, tzinfo=UTC)
        subject = email_subject("Morning brief", when, "America/Chicago")
        try:
            config = make_config()
            config.notify.email = _email_config(server.port)
            with caplog.at_level(logging.INFO, logger="tstd.notify.email"):
                await send(
                    config,
                    _BODY,
                    to="owner@example.com",
                    subject=subject,
                    ssl_context=_client_ctx(),
                )
        finally:
            await server.close()
        auth = next(cmd for cmd in server.commands if cmd.upper().startswith("AUTH"))
        assert _plain_password(auth) == _PASSWORD
        message = _parsed(server)
        assert message["subject"] == subject
        plain = message.get_body(preferencelist=("plain",))
        html = message.get_body(preferencelist=("html",))
        assert plain is not None
        assert plain.get_content().replace("\r\n", "\n").strip() == _BODY
        assert html is not None and "Findings" in html.get_content()
        blob = _logged(caplog)
        assert _PASSWORD not in blob
        assert "FINAL-REPORT-TOKEN" not in blob
        assert any(
            getattr(record, "extra_fields", {}).get("domain") == "example.com"
            for record in caplog.records
        )

    async def test_default_context_refuses_a_self_signed_cert(
        self, tmp_path: Path, password: None
    ) -> None:
        cert, key = _cert(tmp_path)
        server = FakeSmtp(advertise_starttls=True, implicit_tls=False, ctx=_server_ctx(cert, key))
        await server.start()
        try:
            config = make_config()
            config.notify.email = _email_config(server.port)
            with pytest.raises(EmailNotifyError) as caught:
                await send(config, _BODY, to="owner@example.com", subject="t")
        finally:
            await server.close()
        assert _PASSWORD not in str(caught.value)
        assert not any(
            cmd.upper().startswith("AUTH") or cmd.upper() == "DATA" for cmd in server.commands
        )

    async def test_implicit_tls(self, tmp_path: Path, password: None) -> None:
        cert, key = _cert(tmp_path)
        server = FakeSmtp(advertise_starttls=False, implicit_tls=True, ctx=_server_ctx(cert, key))
        await server.start()
        try:
            config = make_config()
            config.notify.email = _email_config(server.port, security="tls")
            await send(
                config,
                "plain report",
                to="owner@example.com",
                subject="Morning brief — 2026-10-01",
                ssl_context=_client_ctx(),
            )
        finally:
            await server.close()
        assert any(cmd.upper().startswith("AUTH") for cmd in server.commands)
        plain = _parsed(server).get_body(preferencelist=("plain",))
        assert plain is not None
        assert plain.get_content().replace("\r\n", "\n").strip() == "plain report"

    async def test_disabled_and_missing_password_are_clear(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = make_config()
        config.notify.email = _email_config(1, enabled=False)
        with pytest.raises(EmailNotifyError, match="disabled"):
            await send(config, _BODY, to="owner@example.com", subject="t")
        config.notify.email = _email_config(1, enabled=True)
        with (
            caplog.at_level(logging.DEBUG),
            pytest.raises(EmailNotifyError, match="not in the keychain") as caught,
        ):
            await send(config, _BODY, to="owner@example.com", subject="t")
        assert "keychain isolated" not in str(caught.value)
        assert _PASSWORD not in _logged(caplog)
        assert "FINAL-REPORT-TOKEN" not in _logged(caplog)

    async def test_auth_failure_redacts_the_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def _get() -> str:
            return "wrong-password"

        monkeypatch.setattr("tstd.keychain.get_smtp_password", _get)
        cert, key = _cert(tmp_path)
        server = FakeSmtp(advertise_starttls=True, implicit_tls=False, ctx=_server_ctx(cert, key))
        await server.start()
        try:
            config = make_config()
            config.notify.email = _email_config(server.port)
            with (
                caplog.at_level(logging.DEBUG, logger="tstd.notify.email"),
                pytest.raises(EmailNotifyError) as caught,
            ):
                await send(
                    config,
                    _BODY,
                    to="owner@example.com",
                    subject="t",
                    ssl_context=_client_ctx(),
                )
        finally:
            await server.close()
        # The fake echoes the password the client sent. It must not come back.
        assert "wrong-password" not in str(caught.value)
        assert _PASSWORD not in str(caught.value)
        assert "FINAL-REPORT-TOKEN" not in str(caught.value)
        assert _PASSWORD not in _logged(caplog)
        assert "FINAL-REPORT-TOKEN" not in _logged(caplog)


class TestSettingsFile:
    def test_a_password_key_is_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="tstd.config"):
            cfg = EmailNotifyConfig.model_validate(
                {"host": "smtp.example.com", "password": _PASSWORD}
            )
        assert "password" not in cfg.model_dump()
        assert _PASSWORD not in caplog.text
        assert "keychain" in caplog.text

    def test_save_never_writes_a_password(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            'notify:\n  slack:\n    enabled: false\n    host: ""\n    timeout_seconds: 5\n',
            encoding="utf-8",
        )
        save_email_notify(
            EmailNotifyConfig(
                enabled=True,
                host="smtp.example.com",
                username="desk",
                from_address="desk@example.com",
            ),
            path,
        )
        text = path.read_text(encoding="utf-8")
        assert "password" not in text
        assert "smtp.example.com" in text

    async def test_empty_password_does_not_replace_the_keychain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stored: list[str] = []

        async def capture(password: str) -> None:
            stored.append(password)

        monkeypatch.setattr("tstd.keychain.store_smtp_password", capture)
        path = tmp_path / "config.yaml"
        path.write_text("notify:\n  slack:\n    enabled: false\n", encoding="utf-8")
        config = make_config()
        await apply_email_notify(
            config,
            SetEmailNotify(
                enabled=True,
                host="smtp.example.com",
                username="desk",
                from_address="desk@example.com",
                password="",
            ),
            path,
        )
        assert stored == []
        await apply_email_notify(
            config,
            SetEmailNotify(
                enabled=True,
                host="smtp.example.com",
                username="desk",
                from_address="desk@example.com",
                password=_PASSWORD,
            ),
            path,
        )
        assert stored == [_PASSWORD]
        assert _PASSWORD not in path.read_text(encoding="utf-8")

    async def test_keychain_error_does_not_echo_the_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(password: str) -> None:
            raise KeychainError(f"refused {password}")

        monkeypatch.setattr("tstd.keychain.store_smtp_password", boom)
        path = tmp_path / "config.yaml"
        path.write_text("notify:\n  slack:\n    enabled: false\n", encoding="utf-8")
        with pytest.raises(EmailNotifyError, match="redacted") as caught:
            await apply_email_notify(
                config := make_config(),
                SetEmailNotify(host="smtp.example.com", password=_PASSWORD),
                path,
            )
        assert _PASSWORD not in str(caught.value)
        assert config.notify.email.host == ""
