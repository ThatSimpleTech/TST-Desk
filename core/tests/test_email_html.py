"""Newsletter HTML for a scheduled report (TD-3821).

The fixture snapshot locks the layout. The attack rows are the model
output: a script, an event attribute, a javascript link, and an image
must not become live HTML. The plain part stays the same Markdown.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from email import message_from_bytes
from email.policy import default as email_default
from pathlib import Path

import pytest

from tests.test_email_notify import FakeSmtp, _cert, _client_ctx, _server_ctx
from tests.test_loop import make_config
from tstd.config import EmailNotifyConfig
from tstd.notify.email import report_chrome, send
from tstd.notify.email_html import render_report_html
from tstd.notify.email_settings import send_test_email

_FIXTURE = Path(__file__).parent / "fixtures" / "email_report.md"
_SNAPSHOT = Path(__file__).parent / "fixtures" / "email_report.html"
_WHEN = datetime(2026, 10, 2, 4, 0, tzinfo=UTC)
_PASSWORD = "s3cret-smtp-pw"
_HEX = re.compile(r"#[0-9a-fA-F]{3,8}")
_TAG = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)\b")
_EVENT = re.compile(r"<[A-Za-z][^>]*\son[a-z]+\s*=", re.IGNORECASE)
_ALLOWED = {
    "html",
    "head",
    "meta",
    "body",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "ul",
    "ol",
    "li",
    "a",
    "strong",
    "em",
    "code",
    "pre",
    "blockquote",
}


def _page(
    markdown: str,
    *,
    title: str = "Morning brief",
    preset: str = "grok",
    when: datetime = _WHEN,
    timezone_name: str | None = "America/Chicago",
) -> str:
    return render_report_html(
        markdown,
        title=title,
        when=when,
        timezone_name=timezone_name,
        preset=preset,
    )


def _assert_safe(page: str) -> None:
    tags = set(_TAG.findall(page))
    assert tags <= _ALLOWED
    assert "<style" not in page.lower()
    assert "<script" not in page.lower()
    assert "<img" not in page.lower()
    assert _EVENT.search(page) is None
    for href in re.findall(r'href="([^"]*)"', page):
        assert href.lower().startswith(("https://", "http://"))


def test_fixture_snapshot_matches_the_newsletter() -> None:
    page = _page(_FIXTURE.read_text(encoding="utf-8"))
    _assert_safe(page)
    assert "<h1" in page and "Morning AI news" in page
    assert "<ul" in page and "<ol" in page
    assert "<table" in page and "<th" in page
    assert 'href="https://example.com/notes"' in page
    assert "<pre" in page and "print(" in page
    assert "<blockquote" in page and "<strong" in page and "<em" in page
    assert "<code" in page
    assert "max-width:680px" in page
    assert "<style" not in page
    assert "Sent by TST Desk · Morning brief · ran 11:00 PM on grok" in page
    assert "October 1, 2026" in page
    assert page == _SNAPSHOT.read_text(encoding="utf-8")


def test_colors_live_in_one_palette() -> None:
    root = Path(__file__).resolve().parents[1] / "tstd" / "notify"
    for name in ("email.py", "email_html.py", "email_inline.py", "email_settings.py"):
        assert _HEX.findall((root / name).read_text(encoding="utf-8")) == []
    source = (root / "email_markdown.py").read_text(encoding="utf-8")
    palette = source.split("PALETTE", 1)[1].split("}", 1)[0]
    assert _HEX.findall(source) == _HEX.findall(palette)


@pytest.mark.parametrize(
    ("source", "visible", "absent"),
    [
        ("<script>alert(1)</script>", "alert(1)", "<script"),
        ('<div onclick="alert(1)">hi</div>', "onclick=", "<div"),
        ("[click](javascript:alert(1))", "click", "javascript:"),
        ("[click](JAVASCRIPT:alert(1))", "click", "javascript:"),
        ('<img src="https://cdn.example/a.png" alt="pic">', "&lt;img", "<img"),
        ("See ![chart](https://cdn.example/a.png) here.", "chart", "cdn.example"),
        ("<style>body{color:red}</style>", "color:red", "<style"),
        ("[who](mailto:owner@example.com)", "who", "mailto:"),
        ("[raw](data:text/html,hi)", "raw", "data:"),
        ('[x](https://example.com/"onclick="alert(1))', "x", "<a "),
    ],
)
def test_untrusted_markup_is_neutralised(source: str, visible: str, absent: str) -> None:
    page = _page(source)
    _assert_safe(page)
    assert visible in page
    assert absent.lower() not in page.lower()


def test_http_and_https_links_are_kept() -> None:
    page = _page("See [notes](https://example.com/a?b=1&c=2) and [plain](http://example.com/p).")
    assert 'href="https://example.com/a?b=1&amp;c=2"' in page
    assert 'href="http://example.com/p"' in page
    assert page.count("<a ") == 2
    mixed = _page("[ok](https://example.com/ok) [bad](javascript:alert(1))")
    assert mixed.count("<a ") == 1
    assert "javascript:" not in mixed.lower()
    wiki = _page("[wiki](https://en.wikipedia.org/wiki/Foo_(bar))")
    assert 'href="https://en.wikipedia.org/wiki/Foo_(bar)"' in wiki


@pytest.mark.parametrize(
    ("source", "tag"),
    [
        ("**bold** and __also__", "strong"),
        ("*italic* and _also_", "em"),
        ("use `code` here", "code"),
    ],
)
def test_emphasis_and_code(source: str, tag: str) -> None:
    page = _page(source)
    assert f"<{tag}" in page
    _assert_safe(page)


def test_words_with_underscores_and_stars_stay_plain() -> None:
    page = _page("file_name and 2*3*4 stay plain")
    assert "<em" not in page
    assert "file_name" in page
    assert "2*3*4" in page


def test_a_hostile_title_is_escaped() -> None:
    page = _page("hello", title="<script>alert(1)</script>", preset='grok" onclick="x')
    _assert_safe(page)
    assert "<script" not in page.lower()
    assert "&lt;script&gt;" in page


def test_chrome_names_the_job_and_falls_back_to_the_active_preset() -> None:
    chrome = report_chrome("Morning brief", _WHEN, "America/Chicago", None, "grok")
    assert chrome.title == "Morning brief"
    assert chrome.subject == "Morning brief — 2026-10-01"
    assert chrome.preset == "grok"
    pinned = report_chrome("Morning brief", _WHEN, "America/Chicago", "research", "grok")
    assert pinned.preset == "research"


@pytest.fixture
def password(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _get() -> str:
        return _PASSWORD

    monkeypatch.setattr("tstd.keychain.get_smtp_password", _get)


def _email_config(port: int) -> EmailNotifyConfig:
    return EmailNotifyConfig(
        enabled=True,
        host="127.0.0.1",
        port=port,
        security="starttls",
        username="desk",
        from_address="desk@example.com",
        timeout_seconds=5,
    )


class TestMultipart:
    async def test_both_parts_and_the_footer(
        self, tmp_path: Path, password: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        cert, key = _cert(tmp_path)
        server = FakeSmtp(advertise_starttls=True, implicit_tls=False, ctx=_server_ctx(cert, key))
        await server.start()
        body = _FIXTURE.read_text(encoding="utf-8")
        try:
            config = make_config()
            config.notify.email = _email_config(server.port)
            with caplog.at_level(logging.INFO, logger="tstd.notify.email"):
                await send(
                    config,
                    body,
                    to="owner@example.com",
                    subject="Morning brief — 2026-10-01",
                    title="Morning brief",
                    when=_WHEN,
                    timezone_name="America/Chicago",
                    preset="grok",
                    ssl_context=_client_ctx(),
                )
        finally:
            await server.close()
        message = message_from_bytes(server.data, policy=email_default)
        assert message.get_content_type() == "multipart/alternative"
        plain = message.get_body(preferencelist=("plain",))
        html = message.get_body(preferencelist=("html",))
        assert plain is not None and html is not None
        assert plain.get_content().replace("\r\n", "\n") == body
        page = html.get_content()
        assert "Morning AI news" in page
        assert "Sent by TST Desk · Morning brief · ran 11:00 PM on grok" in page
        assert _PASSWORD not in caplog.text
        assert "Morning AI news" not in caplog.text

    async def test_send_test_email_uses_the_sample_report(
        self, tmp_path: Path, password: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # send_test_email has no ssl seam. The fake certificate is not a
        # public CA, so this test points the default context at the same
        # non-verifying context the other SMTP tests pass in.
        monkeypatch.setattr(
            "tstd.notify.email.ssl.create_default_context",
            _client_ctx,
        )
        cert, key = _cert(tmp_path)
        server = FakeSmtp(advertise_starttls=True, implicit_tls=False, ctx=_server_ctx(cert, key))
        await server.start()
        try:
            config = make_config()
            config.notify.email = _email_config(server.port)
            result = await send_test_email(config, "owner@example.com")
        finally:
            await server.close()
        assert result.ok is True
        message = message_from_bytes(server.data, policy=email_default)
        assert message["subject"] == "TST Desk test"
        plain = message.get_body(preferencelist=("plain",))
        html = message.get_body(preferencelist=("html",))
        assert plain is not None and html is not None
        text = plain.get_content()
        page = html.get_content()
        assert "# Sample report" in text
        assert "<h1" in page and "Sample report" in page
        assert "<table" in page and 'href="http://localhost/sample"' in page
        assert re.search(
            r"Sent by TST Desk · TST Desk test · ran \d{1,2}:\d{2} [AP]M on test",
            page,
        )
