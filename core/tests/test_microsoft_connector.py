"""Microsoft auth-code + PKCE, loopback, and Graph send (TD-4853).

The fake server is httpx.MockTransport. The loopback test binds a real
127.0.0.1 port and closes it. Nothing here touches the OS keychain or
the public Microsoft hosts.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from collections.abc import Callable
from email import message_from_bytes
from email.policy import default as email_default
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import yaml
from pydantic import ValidationError

import tstd
from tests.test_loop import make_config
from tstd.config import (
    RESERVED_CREDENTIAL_IDS,
    EmailNotifyConfig,
    MicrosoftConnectorConfig,
    ModelConfig,
    default_config_yaml,
)
from tstd.connectors.microsoft_flow import authorize_host_ok, authorize_url, refresh_tokens
from tstd.connectors.microsoft_loopback import LoopbackAuth
from tstd.connectors.microsoft_mail import send_graph
from tstd.connectors.microsoft_models import (
    SCOPES,
    STATE_MESSAGE,
    Callback,
    MicrosoftError,
    MicrosoftTokens,
)
from tstd.connectors.microsoft_pkce import code_challenge, new_pkce, states_match
from tstd.connectors.microsoft_session import MicrosoftCoordinator
from tstd.connectors.microsoft_tokens import MemoryTokenStore, ensure_access
from tstd.notify.email import EmailNotifyError, send
from tstd.protocol import MicrosoftConnect, MicrosoftStatus, SetEmailNotify, parse_client_message

ACCESS = "ms-access-" + "a" * 24
NEW_ACCESS = "ms-access-" + "n" * 24
REFRESH = "ms-refresh-" + "b" * 24
ROTATED = "ms-refresh-" + "r" * 24
CODE = "auth-code-" + "c" * 24
LOGIN = "login.example"
GRAPH = "graph.example"
NOW = 1_700_000_000.0
_RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
_RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def _ms(**over: object) -> MicrosoftConnectorConfig:
    data: dict[str, object] = {
        "client_id": "app-id",
        "tenant": "common",
        "login_host": LOGIN,
        "graph_host": GRAPH,
        "timeout_seconds": 5,
        "sign_in_timeout_seconds": 5,
    }
    data.update(over)
    return MicrosoftConnectorConfig.model_validate(data)


def _config(*, graph_host: str = GRAPH, enabled: bool = True) -> ModelConfig:
    config = make_config()
    config.notify.email = EmailNotifyConfig(
        enabled=enabled,
        method="microsoft",
        from_address="desk@example.com",
        host="smtp.example.com",
        username="desk",
    )
    config.connectors.microsoft = _ms(graph_host=graph_host)
    return config


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


class ScriptedLoopback:
    """Stand-in for the real listener. The opener fills the callback."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.bound_host = host
        self.callback = Callback(code="", state="", error="")
        self.closed = False

    async def start(self) -> str:
        return "http://127.0.0.1:9"

    async def wait(self, timeout_seconds: float) -> Callback:
        del timeout_seconds
        return self.callback

    async def aclose(self) -> None:
        self.closed = True


def _form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


# ── PKCE and config ────────────────────────────────────────────────────


def test_rfc7636_s256_vector() -> None:
    assert code_challenge(_RFC_VERIFIER) == _RFC_CHALLENGE


def test_new_pkce_is_unreserved_and_matches_s256() -> None:
    verifier, challenge = new_pkce()
    assert 43 <= len(verifier) <= 128
    assert re.fullmatch(r"[A-Za-z0-9._~-]+", verifier)
    digest = code_challenge(verifier)
    assert challenge == digest


@pytest.mark.parametrize(
    ("expected", "got", "ok"),
    [
        ("same-state", "same-state", True),
        ("same-state", "same-statf", False),
        ("same-state", "short", False),
        ("", "same-state", False),
        ("same-state", "", False),
        ("", "", False),
    ],
)
def test_states_match(expected: str, got: str, ok: bool) -> None:
    assert states_match(expected, got) is ok


def test_shipped_hosts_come_from_yaml_and_the_connector_starts_off() -> None:
    shipped = ModelConfig.model_validate(yaml.safe_load(default_config_yaml()))
    microsoft = shipped.connectors.microsoft
    assert microsoft.client_id == ""
    assert microsoft.tenant == "common"
    assert microsoft.login_host == "login.microsoftonline.com"
    assert microsoft.graph_host == "graph.microsoft.com"
    assert microsoft.enabled() is False
    bare = MicrosoftConnectorConfig()
    assert bare.login_host == ""
    assert bare.graph_host == ""
    assert bare.tenant == "common"
    assert bare.enabled() is False
    assert "microsoft-oauth" in RESERVED_CREDENTIAL_IDS
    assert EmailNotifyConfig().method == "smtp"


def test_bad_tenant_is_rejected() -> None:
    assert MicrosoftConnectorConfig(tenant="").tenant == "common"
    assert MicrosoftConnectorConfig(tenant="organizations").tenant == "organizations"
    with pytest.raises(ValidationError):
        MicrosoftConnectorConfig(
            tenant="common/evil", client_id="app", login_host=LOGIN, graph_host=GRAPH
        )


def test_connector_python_does_not_name_the_public_hosts() -> None:
    root = Path(tstd.__file__).resolve().parent
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "microsoftonline" not in text, path
        assert "graph.microsoft" not in text, path


def test_authorize_url_stays_on_the_configured_host() -> None:
    url = authorize_url(
        login_host=LOGIN,
        tenant="common",
        client_id="app-id",
        redirect_uri="http://127.0.0.1:9",
        state="state-value",
        code_challenge="challenge",
    )
    assert authorize_host_ok(url, LOGIN) is True
    assert authorize_host_ok(f"https://user@{LOGIN}/oauth", LOGIN) is False
    assert authorize_host_ok(f"http://{LOGIN}/oauth", LOGIN) is False
    assert authorize_host_ok("https://evil.example/oauth", LOGIN) is False


def test_messages_round_trip() -> None:
    parsed = parse_client_message(MicrosoftConnect().model_dump_json())
    assert parsed.type == "microsoft_connect"
    status = MicrosoftStatus(
        enabled=True, connected=True, phase="connected", account="owner@example.com", message=""
    )
    again = MicrosoftStatus.model_validate_json(status.model_dump_json())
    assert again.seq == 1
    assert again.account == "owner@example.com"
    assert SetEmailNotify(method="microsoft").method == "microsoft"


# ── Loopback ───────────────────────────────────────────────────────────


async def _exchange(port: int, target: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        return await reader.read(8192)
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_loopback_binds_localhost_and_one_callback() -> None:
    auth = LoopbackAuth()
    try:
        redirect = await auth.start()
        assert auth.bound_host == "127.0.0.1"
        assert auth.port != 0
        assert redirect == f"http://127.0.0.1:{auth.port}"
        assert auth._server is not None
        for sock in auth._server.sockets or ():
            assert sock.getsockname()[0] == "127.0.0.1"
        missed = await _exchange(auth.port, "/favicon.ico")
        assert b"404" in missed
        assert auth._future is not None and not auth._future.done()
        page = await _exchange(
            auth.port, "/?code=SECRET-CODE&state=STATE&error_description=SECRET-CODE"
        )
        assert b"TST Desk is signed in" in page
        assert b"SECRET-CODE" not in page
        callback = await auth.wait(1)
        assert callback.code == "SECRET-CODE"
        assert callback.state == "STATE"
        assert callback.error == ""
    finally:
        await auth.aclose()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", auth.port)


@pytest.mark.asyncio
async def test_loopback_timeout_closes_the_port() -> None:
    auth = LoopbackAuth()
    await auth.start()
    port = auth.port
    with pytest.raises(MicrosoftError, match="timed out") as exc:
        await auth.wait(0)
    assert exc.value.phase == "expired"
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


# ── Sign-in ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sign_in_exchanges_pkce_and_stores_the_mailbox() -> None:
    store = MemoryTokenStore()
    loopback = ScriptedLoopback()
    opened: list[str] = []
    posted: list[dict[str, list[str]]] = []

    def opener(url: str) -> None:
        opened.append(url)
        query = parse_qs(urlsplit(url).query)
        loopback.callback = Callback(code=CODE, state=query["state"][0], error="")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            assert request.url.host == LOGIN
            form = _form(request)
            posted.append(form)
            assert form["grant_type"] == ["authorization_code"]
            assert form["code"] == [CODE]
            assert "client_secret" not in form
            return httpx.Response(
                200,
                json={"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600},
            )
        assert request.url.host == GRAPH
        assert request.url.path == "/v1.0/me"
        assert request.headers["authorization"] == f"Bearer {ACCESS}"
        return httpx.Response(200, json={"mail": "owner@example.com"})

    coord = MicrosoftCoordinator(
        lambda: _config(),
        broadcast=_done,
        store=store,
        transport=_transport(handler),
        opener=opener,
        loopback_factory=lambda: loopback,
    )
    try:
        pending = await coord.connect()
        assert pending.phase == "pending"
        assert pending.account is None
        assert CODE not in pending.model_dump_json()
        assert coord._task is not None
        await coord._task
        done = await coord.snapshot()
        assert done.phase == "connected"
        assert done.account == "owner@example.com"
        saved = await store.load()
        assert saved is not None
        assert saved.refresh_token == REFRESH
        assert ACCESS not in repr(saved)
        assert loopback.closed is True
        url = opened[0]
        parts = urlsplit(url)
        assert parts.scheme == "https"
        assert parts.hostname == LOGIN
        query = parse_qs(parts.query)
        assert query["response_type"] == ["code"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["scope"] == [SCOPES]
        assert "Mail.Send" in SCOPES
        verifier = posted[0]["code_verifier"][0]
        assert query["code_challenge"] == [code_challenge(verifier)]
        raw = await coord.disconnect_json()
        body = MicrosoftStatus.model_validate_json(raw)
        assert body.phase == "idle"
        assert await store.load() is None
    finally:
        await coord.aclose()


async def _done(_event: MicrosoftStatus) -> None:
    return None


@pytest.mark.asyncio
async def test_state_mismatch_does_not_exchange_the_code(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def opener(url: str) -> None:
        del url
        loopback.callback = Callback(code=CODE, state="not-the-state", error="")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request.url.path)

    loopback = ScriptedLoopback()
    coord = MicrosoftCoordinator(
        lambda: _config(),
        broadcast=_done,
        store=MemoryTokenStore(),
        transport=_transport(handler),
        opener=opener,
        loopback_factory=lambda: loopback,
    )
    try:
        await coord.connect()
        assert coord._task is not None
        await coord._task
        done = await coord.snapshot()
        assert done.phase == "error"
        assert done.message == STATE_MESSAGE
        assert CODE not in done.message
        assert CODE not in caplog.text
    finally:
        await coord.aclose()


@pytest.mark.asyncio
async def test_access_denied_does_not_post_a_token() -> None:
    loopback = ScriptedLoopback()
    loopback.callback = Callback(code=CODE, state="", error="access_denied")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request.url.path)

    coord = MicrosoftCoordinator(
        lambda: _config(),
        broadcast=_done,
        store=MemoryTokenStore(),
        transport=_transport(handler),
        opener=lambda _url: None,
        loopback_factory=lambda: loopback,
    )
    try:
        await coord.connect()
        assert coord._task is not None
        await coord._task
        done = await coord.snapshot()
        assert done.phase == "denied"
        assert "cancelled" in done.message
        assert CODE not in done.message
    finally:
        await coord.aclose()


@pytest.mark.asyncio
async def test_non_loopback_bind_does_not_open_a_browser() -> None:
    opened: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request.url.path)

    coord = MicrosoftCoordinator(
        lambda: _config(),
        broadcast=_done,
        store=MemoryTokenStore(),
        transport=_transport(handler),
        opener=opened.append,
        loopback_factory=lambda: ScriptedLoopback("0.0.0.0"),
    )
    try:
        await coord.connect()
        assert coord._task is not None
        await coord._task
        done = await coord.snapshot()
        assert done.phase == "error"
        assert opened == []
        assert "0.0.0.0" not in done.message
    finally:
        await coord.aclose()


@pytest.mark.asyncio
async def test_browser_failure_hides_the_exception_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secret = "https://login.example/leak-" + CODE

    def opener(url: str) -> None:
        del url
        raise RuntimeError(secret)

    coord = MicrosoftCoordinator(
        lambda: _config(),
        broadcast=_done,
        store=MemoryTokenStore(),
        transport=_transport(lambda _request: httpx.Response(500)),
        opener=opener,
        loopback_factory=ScriptedLoopback,
    )
    try:
        await coord.connect()
        assert coord._task is not None
        await coord._task
        done = await coord.snapshot()
        assert done.message == "Could not open a browser."
        assert secret not in done.message
        assert CODE not in caplog.text
        assert secret not in caplog.text
    finally:
        await coord.aclose()


# ── Refresh and Graph ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_rotates_once_and_hides_the_error_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    store = MemoryTokenStore()
    await store.save(MicrosoftTokens(ACCESS, REFRESH, NOW))
    calls: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == LOGIN
        form = _form(request)
        calls.append(form)
        assert form["grant_type"] == ["refresh_token"]
        assert "client_secret" not in form
        return httpx.Response(
            200, json={"access_token": NEW_ACCESS, "refresh_token": ROTATED, "expires_in": 3600}
        )

    token = await ensure_access(_ms(), store, now=lambda: NOW, transport=_transport(handler))
    assert token == NEW_ACCESS
    saved = await store.load()
    assert saved is not None
    assert saved.refresh_token == ROTATED
    again = await ensure_access(_ms(), store, now=lambda: NOW, transport=_transport(handler))
    assert again == NEW_ACCESS
    assert len(calls) == 1

    def leak(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": ACCESS})

    await store.save(MicrosoftTokens(ACCESS, REFRESH, NOW))
    with pytest.raises(MicrosoftError) as exc:
        await refresh_tokens(
            login_host=LOGIN,
            tenant="common",
            client_id="app-id",
            refresh_token=REFRESH,
            timeout_seconds=5,
            transport=_transport(leak),
            wall=lambda: NOW,
        )
    assert ACCESS not in str(exc.value)
    assert ACCESS not in caplog.text

    def boom(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError(f"boom {REFRESH}")

    await store.save(MicrosoftTokens(ACCESS, REFRESH, NOW))
    with pytest.raises(MicrosoftError) as blown:
        await ensure_access(_ms(), store, now=lambda: NOW, transport=_transport(boom))
    assert REFRESH not in blown.value.message
    assert ACCESS not in blown.value.message
    assert "[redacted]" in blown.value.message


@pytest.mark.asyncio
async def test_graph_send_posts_mime_and_drops_the_error_body() -> None:
    store = MemoryTokenStore()
    fresh = time.time() + 3600
    await store.save(MicrosoftTokens(ACCESS, REFRESH, fresh))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/sendMail") and len(seen) == 1:
            return httpx.Response(400, content=f"nope {ACCESS}".encode())
        return httpx.Response(202)

    with pytest.raises(EmailNotifyError) as exc:
        await send(
            _config(),
            "PLAIN-REPORT-LINE",
            to="owner@example.com",
            subject="Job",
            token_store=store,
            transport=_transport(handler),
        )
    assert exc.value.error_class == "MicrosoftError"
    assert ACCESS not in str(exc.value)
    assert "HTTP 400" in exc.value.message
    failed = seen[0]
    assert failed.url.host == GRAPH
    assert failed.url.path == "/v1.0/me/sendMail"
    assert ACCESS not in failed.content.decode()

    seen.clear()

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200,
                json={"access_token": NEW_ACCESS, "refresh_token": ROTATED, "expires_in": 3600},
            )
        if len([item for item in seen if item.url.path.endswith("/sendMail")]) == 1:
            return httpx.Response(401, content=ACCESS.encode())
        return httpx.Response(202)

    await store.save(MicrosoftTokens(ACCESS, REFRESH, fresh))
    await send_graph(
        _config(graph_host="graph.other.example"),
        to="owner@example.com",
        subject="Job",
        body="PLAIN-REPORT-LINE",
        html_body="<p>HTML-REPORT-LINE</p>",
        from_address="desk@example.com",
        store=store,
        transport=_transport(ok),
    )
    mails = [item for item in seen if item.url.path.endswith("/sendMail")]
    assert [item.url.host for item in mails] == ["graph.other.example", "graph.other.example"]
    assert mails[0].headers["authorization"] == f"Bearer {ACCESS}"
    assert mails[1].headers["authorization"] == f"Bearer {NEW_ACCESS}"
    mime = message_from_bytes(base64.b64decode(mails[1].content), policy=email_default)
    parts = {
        part.get_content_type(): part.get_content()
        for part in mime.walk()
        if not part.is_multipart()
    }
    assert "PLAIN-REPORT-LINE" in parts["text/plain"]
    assert "HTML-REPORT-LINE" in parts["text/html"]
    assert ACCESS not in mails[1].content.decode()
    assert NEW_ACCESS not in mails[1].content.decode()
