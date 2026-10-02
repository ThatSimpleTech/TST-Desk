"""Google auth-code + PKCE, Gmail send, and invalid_grant (TD-4854).

The fake server is httpx.MockTransport. Nothing here touches the OS
keychain or the public Google hosts. The loopback listener is the one
TD-4853 already binds to 127.0.0.1.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable, Iterator
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
    GoogleConnectorConfig,
    ModelConfig,
    default_config_yaml,
)
from tstd.connectors.google_expiry import clear_expired, current_expired, mark_expired
from tstd.connectors.google_flow import (
    authorize_host_ok,
    authorize_url,
    exchange_code,
    refresh_tokens,
)
from tstd.connectors.google_mail import encode_mime, send_gmail
from tstd.connectors.google_models import (
    EXPIRED_MESSAGE,
    SCOPES,
    SECRET_MESSAGE,
    STATE_MESSAGE,
    GoogleError,
    GoogleTokens,
)
from tstd.connectors.google_session import GoogleCoordinator, state_from_config
from tstd.connectors.google_tokens import (
    KeychainSecretStore,
    KeychainTokenStore,
    MemorySecretStore,
    MemoryTokenStore,
    ensure_access,
)
from tstd.connectors.microsoft_loopback import LoopbackAuth
from tstd.connectors.microsoft_models import Callback
from tstd.connectors.microsoft_pkce import code_challenge
from tstd.notify.email import EmailNotifyError, send
from tstd.notify.email_settings import apply_email_notify
from tstd.protocol import (
    GoogleConnect,
    GoogleStatus,
    SetEmailNotify,
    parse_client_message,
)

ACCESS = "gg-access-" + "a" * 24
NEW_ACCESS = "gg-access-" + "n" * 24
REFRESH = "gg-refresh-" + "b" * 24
CODE = "auth-code-" + "c" * 24
SECRET = "gg-secret-" + "s" * 24
AUTH = "auth.example"
TOKEN = "token.example"
USERINFO = "userinfo.example"
GMAIL = "gmail.example"
NOW = 1_700_000_000.0
_FORBIDDEN_HOSTS = (
    "accounts.google.com",
    "oauth2.googleapis.com",
    "gmail.googleapis.com",
    "openidconnect.googleapis.com",
)


@pytest.fixture(autouse=True)
def _reset_expiry() -> Iterator[None]:
    # The flag and the coordinator hooks are process-local. A failed test
    # must not leave either for the next one.
    from tstd.connectors import google_expiry

    google_expiry._hooks.clear()
    clear_expired()
    yield
    google_expiry._hooks.clear()
    clear_expired()


def _google(**over: object) -> GoogleConnectorConfig:
    data: dict[str, object] = {
        "client_id": "app-id",
        "auth_host": AUTH,
        "token_host": TOKEN,
        "userinfo_host": USERINFO,
        "gmail_host": GMAIL,
        "timeout_seconds": 5,
        "sign_in_timeout_seconds": 5,
    }
    data.update(over)
    return GoogleConnectorConfig.model_validate(data)


def _config(*, gmail_host: str = GMAIL, enabled: bool = True) -> ModelConfig:
    config = make_config()
    config.notify.email = EmailNotifyConfig(
        enabled=enabled,
        method="google",
        from_address="desk@example.com",
        host="smtp.example.com",
        username="desk",
    )
    config.connectors.google = _google(gmail_host=gmail_host)
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


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError(request.url.path)


def _jwt(email: str) -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"email": email, "email_verified": True}).encode())
        .decode("ascii")
        .rstrip("=")
    )
    return f"e30.{payload}.sig"


def _coord(
    store: MemoryTokenStore,
    secrets: MemorySecretStore,
    handler: Callable[[httpx.Request], httpx.Response],
    opener: Callable[[str], object],
    loopback: ScriptedLoopback,
    events: list[GoogleStatus] | None = None,
) -> GoogleCoordinator:
    async def broadcast(event: GoogleStatus) -> None:
        if events is not None:
            events.append(event)

    return GoogleCoordinator(
        lambda: _config(),
        broadcast,
        store=store,
        secrets=secrets,
        transport=_transport(handler),
        opener=opener,
        loopback_factory=lambda: loopback,
    )


# ── Config and hosts ───────────────────────────────────────────────────


def test_shipped_hosts_come_from_yaml_and_the_connector_starts_off() -> None:
    shipped = ModelConfig.model_validate(yaml.safe_load(default_config_yaml()))
    google = shipped.connectors.google
    assert google.client_id == ""
    assert google.auth_host == "accounts.google.com"
    assert google.token_host == "oauth2.googleapis.com"
    assert google.userinfo_host == "openidconnect.googleapis.com"
    assert google.gmail_host == "gmail.googleapis.com"
    assert google.enabled() is False
    bare = GoogleConnectorConfig()
    assert bare.auth_host == ""
    assert bare.token_host == ""
    assert bare.userinfo_host == ""
    assert bare.gmail_host == ""
    assert bare.enabled() is False
    assert "google-oauth" in RESERVED_CREDENTIAL_IDS
    assert "google-client-secret" in RESERVED_CREDENTIAL_IDS
    assert EmailNotifyConfig().method == "smtp"


def test_client_secret_in_yaml_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    cfg = GoogleConnectorConfig.model_validate(
        {
            "client_id": "app-id",
            "auth_host": AUTH,
            "token_host": TOKEN,
            "userinfo_host": USERINFO,
            "gmail_host": GMAIL,
            "client_secret": SECRET,
        }
    )
    assert cfg.enabled() is True
    assert "client_secret" not in cfg.model_dump()
    assert SECRET not in caplog.text
    assert "keychain" in caplog.text


def test_a_url_is_not_a_host() -> None:
    with pytest.raises(ValidationError):
        GoogleConnectorConfig(auth_host="https://auth.example/path")


def test_connector_python_does_not_name_the_public_hosts() -> None:
    root = Path(tstd.__file__).resolve().parent
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for host in _FORBIDDEN_HOSTS:
            assert host not in text, path


def test_authorize_url_stays_on_the_configured_host() -> None:
    url = authorize_url(
        auth_host=AUTH,
        client_id="app-id",
        redirect_uri="http://127.0.0.1:9",
        state="state-value",
        code_challenge="challenge",
    )
    assert authorize_host_ok(url, AUTH) is True
    assert SECRET not in url
    assert "client_secret" not in url
    query = parse_qs(urlsplit(url).query)
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["scope"] == [SCOPES]
    assert "gmail.send" in SCOPES
    assert authorize_host_ok(f"https://user@{AUTH}/o/oauth2/v2/auth", AUTH) is False
    assert authorize_host_ok(f"http://{AUTH}/o/oauth2/v2/auth", AUTH) is False


def test_messages_round_trip() -> None:
    parsed = parse_client_message(GoogleConnect().model_dump_json())
    assert parsed.type == "google_connect"
    status = GoogleStatus(
        enabled=True,
        connected=True,
        phase="connected",
        account="owner@example.com",
        message="",
        client_secret_stored=True,
    )
    again = GoogleStatus.model_validate_json(status.model_dump_json())
    assert again.seq == 1
    assert again.account == "owner@example.com"
    assert again.client_secret_stored is True
    assert SetEmailNotify(method="google").method == "google"


def test_mime_is_unpadded_base64url() -> None:
    assert encode_mime(b"\xfb\xff") == "-_8"
    assert "=" not in encode_mime(b"hello")


@pytest.mark.asyncio
async def test_the_default_listener_is_the_microsoft_loopback() -> None:
    coord = GoogleCoordinator(lambda: _config(), _ignore)
    try:
        assert coord._loopback_factory is LoopbackAuth
    finally:
        await coord.aclose()


# ── Sign-in ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sign_in_exchanges_pkce_and_prefers_userinfo() -> None:
    store = MemoryTokenStore()
    secrets = MemorySecretStore(SECRET)
    loopback = ScriptedLoopback()
    opened: list[str] = []
    events: list[GoogleStatus] = []
    id_token = _jwt("other@example.com")

    def opener(url: str) -> None:
        opened.append(url)
        query = parse_qs(urlsplit(url).query)
        loopback.callback = Callback(code=CODE, state=query["state"][0], error="")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            assert request.url.host == TOKEN
            form = _form(request)
            assert form["grant_type"] == ["authorization_code"]
            assert form["code"] == [CODE]
            assert form["client_secret"] == [SECRET]
            assert form["code_verifier"]
            return httpx.Response(
                200,
                json={
                    "access_token": ACCESS,
                    "refresh_token": REFRESH,
                    "expires_in": 3600,
                    "id_token": id_token,
                },
            )
        assert request.url.host == USERINFO
        assert request.url.path == "/v1/userinfo"
        assert request.headers["authorization"] == f"Bearer {ACCESS}"
        return httpx.Response(200, json={"email": "owner@example.com", "email_verified": True})

    coord = _coord(store, secrets, handler, opener, loopback, events)
    try:
        pending = await coord.connect()
        assert pending.phase == "pending"
        assert pending.account is None
        assert SECRET not in pending.model_dump_json()
        assert coord._task is not None
        await coord._task
        done = await coord.snapshot()
        assert done.phase == "connected"
        assert done.account == "owner@example.com"
        assert done.client_secret_stored is True
        saved = await store.load()
        assert saved is not None
        assert saved.refresh_token == REFRESH
        blob = saved.access_token + saved.refresh_token
        assert "id_token" not in blob
        assert id_token not in blob
        assert ACCESS not in repr(saved)
        assert loopback.closed is True
        url = opened[0]
        query = parse_qs(urlsplit(url).query)
        assert query["response_type"] == ["code"]
        assert query["code_challenge_method"] == ["S256"]
        assert urlsplit(url).hostname == AUTH
        assert SECRET not in url
        assert query["code_challenge"][0]
        raw = events[-1].model_dump_json()
        assert SECRET not in raw
        assert ACCESS not in raw
        assert CODE not in raw
        assert id_token not in raw
        body = GoogleStatus.model_validate_json(await coord.disconnect_json())
        assert body.phase == "idle"
        assert await store.load() is None
        assert await secrets.load() == SECRET
    finally:
        await coord.aclose()


@pytest.mark.asyncio
async def test_sign_in_posts_the_pkce_verifier() -> None:
    store = MemoryTokenStore()
    secrets = MemorySecretStore(SECRET)
    loopback = ScriptedLoopback()
    posted: list[dict[str, list[str]]] = []

    def opener(url: str) -> None:
        query = parse_qs(urlsplit(url).query)
        loopback.callback = Callback(code=CODE, state=query["state"][0], error="")
        posted.append({"challenge": query["code_challenge"]})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            posted.append(_form(request))
            return httpx.Response(
                200,
                json={"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600},
            )
        return httpx.Response(500)

    coord = _coord(store, secrets, handler, opener, loopback)
    try:
        await coord.connect()
        assert coord._task is not None
        await coord._task
        done = await coord.snapshot()
        assert done.phase == "connected"
        verifier = posted[1]["code_verifier"][0]
        assert posted[0]["challenge"] == [code_challenge(verifier)]
    finally:
        await coord.aclose()


@pytest.mark.asyncio
async def test_missing_secret_does_not_open_a_browser() -> None:
    opened: list[str] = []
    coord = _coord(
        MemoryTokenStore(),
        MemorySecretStore(""),
        lambda _request: httpx.Response(500),
        opened.append,
        ScriptedLoopback(),
    )
    try:
        pending = await coord.connect()
        assert pending.phase == "error"
        assert pending.message == SECRET_MESSAGE
        assert opened == []
        assert coord._task is None
    finally:
        await coord.aclose()


@pytest.mark.asyncio
async def test_state_mismatch_does_not_exchange_the_code(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    loopback = ScriptedLoopback()

    def opener(url: str) -> None:
        del url
        loopback.callback = Callback(code=CODE, state="not-the-state", error="")

    coord = _coord(
        MemoryTokenStore(),
        MemorySecretStore(SECRET),
        _refuse,
        opener,
        loopback,
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
        assert SECRET not in caplog.text
    finally:
        await coord.aclose()


@pytest.mark.asyncio
async def test_access_denied_does_not_post_a_token() -> None:
    loopback = ScriptedLoopback()
    loopback.callback = Callback(code=CODE, state="", error="access_denied")
    coord = _coord(
        MemoryTokenStore(),
        MemorySecretStore(SECRET),
        _refuse,
        lambda _url: None,
        loopback,
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
    coord = GoogleCoordinator(
        lambda: _config(),
        _ignore,
        store=MemoryTokenStore(),
        secrets=MemorySecretStore(SECRET),
        transport=_transport(_refuse),
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
    secret = "https://auth.example/leak-" + CODE

    def opener(url: str) -> None:
        del url
        raise RuntimeError(secret)

    coord = _coord(
        MemoryTokenStore(),
        MemorySecretStore(SECRET),
        lambda _request: httpx.Response(500),
        opener,
        ScriptedLoopback(),
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


@pytest.mark.asyncio
async def test_code_exchange_without_a_refresh_token_is_not_saved() -> None:
    loopback = ScriptedLoopback()

    def opener(url: str) -> None:
        query = parse_qs(urlsplit(url).query)
        loopback.callback = Callback(code=CODE, state=query["state"][0], error="")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/token"
        return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 3600})

    store = MemoryTokenStore()
    coord = _coord(store, MemorySecretStore(SECRET), handler, opener, loopback)
    try:
        await coord.connect()
        assert coord._task is not None
        await coord._task
        done = await coord.snapshot()
        assert done.phase == "expired"
        assert "reconnect" in done.message
        assert await store.load() is None
    finally:
        await coord.aclose()


# ── Refresh, expiry, Gmail ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_keeps_the_old_token_and_hides_the_error_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    store = MemoryTokenStore()
    secrets = MemorySecretStore(SECRET)
    await store.save(GoogleTokens(ACCESS, REFRESH, NOW))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == TOKEN
        form = _form(request)
        assert form["grant_type"] == ["refresh_token"]
        assert form["client_secret"] == [SECRET]
        return httpx.Response(200, json={"access_token": NEW_ACCESS, "expires_in": 3600})

    token = await ensure_access(
        _google(), store, secrets, now=lambda: NOW, transport=_transport(handler)
    )
    assert token == NEW_ACCESS
    saved = await store.load()
    assert saved is not None
    assert saved.refresh_token == REFRESH
    again = await ensure_access(
        _google(), store, secrets, now=lambda: NOW, transport=_transport(handler)
    )
    assert again == NEW_ACCESS

    def leak(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": ACCESS + SECRET}
        )

    with pytest.raises(GoogleError) as exc:
        await refresh_tokens(
            token_host=TOKEN,
            client_id="app-id",
            client_secret=SECRET,
            refresh_token=REFRESH,
            timeout_seconds=5,
            transport=_transport(leak),
            wall=lambda: NOW,
        )
    assert exc.value.phase == "expired"
    assert exc.value.message == EXPIRED_MESSAGE
    assert ACCESS not in str(exc.value)
    assert SECRET not in str(exc.value)
    assert ACCESS not in caplog.text

    def boom(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError(f"boom {REFRESH} {SECRET}")

    await store.save(GoogleTokens(ACCESS, REFRESH, NOW))
    with pytest.raises(GoogleError) as blown:
        await ensure_access(_google(), store, secrets, now=lambda: NOW, transport=_transport(boom))
    assert REFRESH not in blown.value.message
    assert SECRET not in blown.value.message
    assert "[redacted]" in blown.value.message


@pytest.mark.asyncio
async def test_invalid_grant_marks_expired_and_keeps_the_tokens() -> None:
    store = MemoryTokenStore()
    secrets = MemorySecretStore(SECRET)
    await store.save(GoogleTokens(ACCESS, REFRESH, NOW))
    events: list[GoogleStatus] = []

    async def broadcast(event: GoogleStatus) -> None:
        events.append(event)

    coord = GoogleCoordinator(
        lambda: _config(),
        broadcast,
        store=store,
        secrets=secrets,
    )
    try:

        def leak(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "leaked-" + ACCESS}
            )

        with pytest.raises(GoogleError) as exc:
            await ensure_access(
                _google(), store, secrets, now=lambda: NOW, transport=_transport(leak)
            )
        assert exc.value.phase == "expired"
        assert "reconnect" in exc.value.message
        assert "testing" in exc.value.message
        assert "unverified" in exc.value.message
        assert ACCESS not in exc.value.message
        saved = await store.load()
        assert saved is not None
        assert saved.refresh_token == REFRESH
        assert current_expired() == EXPIRED_MESSAGE
        assert events[-1].phase == "expired"
        assert events[-1].connected is True
        assert ACCESS not in events[-1].model_dump_json()

        fresh = GoogleCoordinator(lambda: _config(), _ignore, store=store, secrets=secrets)
        try:
            snap = await fresh.snapshot()
            assert snap.phase == "expired"
            assert snap.connected is True
        finally:
            await fresh.aclose()

        async def stored(self: object) -> bool:
            del self
            return True

        # A later process reads the flag without a secret read of the blob.
        monkey_store = KeychainTokenStore.stored
        monkey_secret = KeychainSecretStore.stored
        KeychainTokenStore.stored = stored  # type: ignore[method-assign]
        KeychainSecretStore.stored = stored  # type: ignore[method-assign]
        try:
            seen = await state_from_config(_config())
        finally:
            KeychainTokenStore.stored = monkey_store  # type: ignore[method-assign]
            KeychainSecretStore.stored = monkey_secret  # type: ignore[method-assign]
        assert seen.phase == "expired"
        assert seen.connected is True
        assert ACCESS not in seen.model_dump_json()
    finally:
        await coord.aclose()


@pytest.mark.asyncio
async def test_a_later_refresh_clears_the_expired_flag() -> None:
    store = MemoryTokenStore()
    secrets = MemorySecretStore(SECRET)
    await store.save(GoogleTokens(ACCESS, REFRESH, NOW))
    await mark_expired(EXPIRED_MESSAGE)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"access_token": NEW_ACCESS, "expires_in": 3600})

    token = await ensure_access(
        _google(), store, secrets, now=lambda: NOW, transport=_transport(handler)
    )
    assert token == NEW_ACCESS
    assert current_expired() is None
    coord = GoogleCoordinator(lambda: _config(), _ignore, store=store, secrets=secrets)
    try:
        snap = await coord.snapshot()
        assert snap.phase == "connected"
    finally:
        await coord.aclose()


@pytest.mark.asyncio
async def test_redirect_is_not_followed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(302, headers={"location": "https://evil.example/token"})

    with pytest.raises(GoogleError) as exc:
        await exchange_code(
            token_host=TOKEN,
            client_id="app-id",
            client_secret=SECRET,
            code=CODE,
            redirect_uri="http://127.0.0.1:9",
            code_verifier="verifier-value",
            timeout_seconds=5,
            transport=_transport(handler),
            wall=lambda: NOW,
        )
    assert SECRET not in str(exc.value)
    assert CODE not in str(exc.value)
    assert SECRET not in caplog.text
    assert "evil.example" not in caplog.text


@pytest.mark.asyncio
async def test_gmail_send_posts_raw_mime_and_retries_once() -> None:
    store = MemoryTokenStore()
    secrets = MemorySecretStore(SECRET)
    fresh = time.time() + 3600
    await store.save(GoogleTokens(ACCESS, REFRESH, fresh))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/messages/send") and len(seen) == 1:
            return httpx.Response(400, content=f"nope {ACCESS}".encode())
        return httpx.Response(200)

    with pytest.raises(EmailNotifyError) as exc:
        await send(
            _config(),
            "PLAIN-REPORT-LINE",
            to="owner@example.com",
            subject="Job",
            token_store=store,
            secret_store=secrets,
            transport=_transport(handler),
        )
    assert exc.value.error_class == "GoogleError"
    assert ACCESS not in str(exc.value)
    assert "HTTP 400" in exc.value.message
    failed = seen[0]
    assert failed.url.host == GMAIL
    assert failed.url.path == "/gmail/v1/users/me/messages/send"
    assert ACCESS not in failed.content.decode()

    seen.clear()

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": NEW_ACCESS, "expires_in": 3600})
        sends = [item for item in seen if item.url.path.endswith("/messages/send")]
        if len(sends) == 1:
            return httpx.Response(401, content=ACCESS.encode())
        return httpx.Response(200)

    await store.save(GoogleTokens(ACCESS, REFRESH, fresh))
    await send_gmail(
        _config(gmail_host="gmail.other.example"),
        to="owner@example.com",
        subject="Job",
        body="PLAIN-REPORT-LINE",
        html_body="<p>HTML-REPORT-LINE</p>",
        from_address="desk@example.com",
        store=store,
        secrets=secrets,
        transport=_transport(ok),
    )
    mails = [item for item in seen if item.url.path.endswith("/messages/send")]
    assert [item.url.host for item in mails] == ["gmail.other.example", "gmail.other.example"]
    assert mails[0].headers["authorization"] == f"Bearer {ACCESS}"
    assert mails[1].headers["authorization"] == f"Bearer {NEW_ACCESS}"
    raw = json.loads(mails[1].content.decode())["raw"]
    assert "=" not in raw
    pad = "=" * (-len(raw) % 4)
    mime = message_from_bytes(base64.urlsafe_b64decode(raw + pad), policy=email_default)
    parts = {
        part.get_content_type(): part.get_content()
        for part in mime.walk()
        if not part.is_multipart()
    }
    assert "PLAIN-REPORT-LINE" in parts["text/plain"]
    assert "HTML-REPORT-LINE" in parts["text/html"]
    assert ACCESS not in mails[1].content.decode()
    assert all(item.url.path != "/v1/userinfo" for item in seen)


@pytest.mark.asyncio
async def test_gmail_accepts_only_200_and_invalid_grant_on_retry() -> None:
    store = MemoryTokenStore()
    secrets = MemorySecretStore(SECRET)
    await store.save(GoogleTokens(ACCESS, REFRESH, time.time() + 3600))

    def accepted(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(202)

    with pytest.raises(GoogleError, match="HTTP 202"):
        await send_gmail(
            _config(),
            to="owner@example.com",
            subject="Job",
            body="plain",
            html_body="<p>html</p>",
            from_address="desk@example.com",
            store=store,
            secrets=secrets,
            transport=_transport(accepted),
        )

    def dead(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages/send"):
            return httpx.Response(401)
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": ACCESS})

    with pytest.raises(EmailNotifyError) as exc:
        await send(
            _config(),
            "plain",
            to="owner@example.com",
            subject="Job",
            token_store=store,
            secret_store=secrets,
            transport=_transport(dead),
        )
    assert "reconnect" in exc.value.message
    assert "HTTP 401" not in exc.value.message
    assert ACCESS not in exc.value.message
    assert await store.load() is not None


@pytest.mark.asyncio
async def test_settings_store_the_secret_before_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored: list[str] = []

    async def capture(secret: str) -> None:
        stored.append(secret)

    path = tmp_path / "config.yaml"
    path.write_text("notify:\n  slack:\n    enabled: false\n", encoding="utf-8")
    config = make_config()
    monkeypatch.setattr("tstd.keychain.store_google_client_secret", capture)
    await apply_email_notify(
        config,
        SetEmailNotify(enabled=True, method="google", from_address="desk@example.com"),
        path,
    )
    assert stored == []
    text = path.read_text(encoding="utf-8")
    assert "method: google" in text
    assert "client_secret" not in text
    await apply_email_notify(
        config,
        SetEmailNotify(
            enabled=True,
            method="google",
            from_address="desk@example.com",
            google_client_secret=SECRET,
        ),
        path,
    )
    assert stored == [SECRET]
    assert SECRET not in path.read_text(encoding="utf-8")


async def _ignore(_event: GoogleStatus) -> None:
    return None
