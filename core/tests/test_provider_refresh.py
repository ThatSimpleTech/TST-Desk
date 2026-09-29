"""A rotated or newly stored API key is used without restarting (TD-4840).

The session loop keeps the provider handle it first received, so dropping
the daemon's cache entry is not enough. These tests drive that handle:
a scripted client for the refresh rules, a real httpx client for close
and the Authorization header, and one agent-loop turn for the wire.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from tstd.config import CredentialConfig, ModelConfig, Preset, TierConfig
from tstd.daemon import Daemon
from tstd.keychain import KeychainError
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.protocol import AssistantDelta, TurnComplete
from tstd.provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Delta,
    ProviderClient,
    ProviderError,
    RetryConfig,
    StreamChunk,
)
from tstd.provider_refresh import (
    API_KEY_REJECTED,
    RefreshingProvider,
    auth_status,
    host_label,
    is_auth_failure,
    key_digest,
    rejected_key_message,
)
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner

OLD = "ezer-old-token"
NEW = "ezer-new-token"
HOST = "http://192.0.2.49:4000/v1"
EXACT_401 = "EZER (192.0.2.49:4000) rejected the API key (401) — check it in Settings → API keys"
EXACT_403 = "EZER (192.0.2.49:4000) rejected the API key (403) — check it in Settings → API keys"

OK_BODY = {
    "id": "c1",
    "model": "m",
    "choices": [
        {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"},
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def _request() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="ezer-brain",
        messages=[ChatMessage(role="user", content="hi")],
    )


def _auth(status: int, code: str) -> ProviderError:
    return ProviderError(code=code, message="rejected", status_code=status, retryable=False)


def _ok_response() -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="1",
        model="m",
        message=ChatMessage(role="assistant", content="ok"),
        finish_reason="stop",
    )


def _ok_chunk() -> StreamChunk:
    return StreamChunk(id="1", delta=Delta(content="ok"), finish_reason="stop")


def _log_blob(caplog: pytest.LogCaptureFixture) -> str:
    """Our log lines plus structured fields. ``caplog.text`` omits ``extra``.

    Library loggers stay out of this blob: a debug httpx line can echo a
    header, and that is not a message this process wrote.
    """
    parts: list[str] = []
    for record in caplog.records:
        if not record.name.startswith("tstd"):
            continue
        parts.append(record.getMessage())
        extra = getattr(record, "extra_fields", None)
        if extra is not None:
            parts.append(repr(extra))
        if record.exc_text:
            parts.append(record.exc_text)
    return "\n".join(parts)


def _assert_secret_free(*chunks: object) -> None:
    text = "\n".join(str(chunk) for chunk in chunks)
    for secret in (OLD, NEW, key_digest(OLD), key_digest(NEW)):
        assert secret not in text


class _Box:
    """One client with a fixed key and one scripted result."""

    def __init__(self, api_key: str, outcome: object) -> None:
        self.api_key = api_key
        self.outcome = outcome
        self.calls = 0
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    def _take(self) -> object:
        self.calls += 1
        return self.outcome

    async def chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse | ProviderError:
        del request
        outcome = self._take()
        assert isinstance(outcome, (ChatCompletionResponse, ProviderError))
        return outcome

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[StreamChunk | ProviderError]:
        del request
        outcome = self._take()
        if isinstance(outcome, list):
            for item in outcome:
                yield item
            return
        assert isinstance(outcome, (StreamChunk, ProviderError))
        yield outcome


def _handle(
    first: _Box,
    rebuilds: list[_Box],
    *,
    name: str = "EZER",
    base_url: str = HOST,
) -> RefreshingProvider:
    async def rebuild() -> _Box:
        return rebuilds.pop(0)

    return RefreshingProvider(
        first,
        credential_id="ezer",
        credential_name=name,
        base_url=base_url,
        rebuild=rebuild,
    )


class TestLabels:
    @pytest.mark.parametrize(
        ("url", "label"),
        [
            ("http://192.0.2.49:4000/v1", "192.0.2.49:4000"),
            ("https://openrouter.ai/api/v1", "openrouter.ai"),
            ("http://[::1]:4000/v1", "[::1]:4000"),
        ],
    )
    def test_host_label_drops_scheme_and_path(self, url: str, label: str) -> None:
        assert host_label(url) == label

    def test_rejected_sentence_names_credential_host_and_status(self) -> None:
        assert rejected_key_message("EZER", HOST, 401) == EXACT_401
        assert rejected_key_message("EZER", HOST, 403) == EXACT_403
        assert "http://" not in EXACT_401
        assert "/v1" not in EXACT_401

    def test_digest_is_not_the_key(self) -> None:
        digest = key_digest(OLD)
        assert len(digest) == 64
        assert digest != OLD
        assert key_digest(None) == ""
        assert key_digest("") == ""
        assert key_digest(OLD) != key_digest(NEW)

    @pytest.mark.parametrize(
        ("status", "code", "auth"),
        [
            (401, "auth_failed", True),
            (403, "forbidden", True),
            (0, "auth_failed", True),
            (0, "forbidden", True),
            (500, "server_error", False),
            (402, "insufficient_credits", False),
        ],
    )
    def test_auth_failure_is_401_403_or_those_codes(
        self, status: int, code: str, auth: bool
    ) -> None:
        error = _auth(status, code)
        assert is_auth_failure(error) is auth

    @pytest.mark.parametrize(
        ("status", "code", "shown"),
        [
            (401, "auth_failed", 401),
            (403, "forbidden", 403),
            (0, "auth_failed", 401),
            (0, "forbidden", 403),
        ],
    )
    def test_auth_status_names_a_code_that_arrived_without_one(
        self, status: int, code: str, shown: int
    ) -> None:
        assert auth_status(_auth(status, code)) == shown


class TestHandleRefresh:
    async def test_changed_key_retries_the_stream_once(self) -> None:
        first = _Box(OLD, _auth(401, "auth_failed"))
        second = _Box(NEW, _ok_chunk())
        handle = _handle(first, [second])

        items = [item async for item in handle.chat_completion_stream(_request())]

        assert items == [_ok_chunk()]
        assert first.closed is True
        assert first.calls == 1
        assert second.calls == 1
        assert second.closed is False
        await handle.close()
        assert second.closed is True

    @pytest.mark.parametrize(
        ("status", "code", "sentence"),
        [
            (401, "auth_failed", EXACT_401),
            (403, "forbidden", EXACT_403),
            (0, "auth_failed", EXACT_401),
            (0, "forbidden", EXACT_403),
        ],
    )
    async def test_unchanged_key_is_not_retried(
        self, status: int, code: str, sentence: str
    ) -> None:
        first = _Box(OLD, _auth(status, code))
        second = _Box(OLD, _ok_response())
        handle = _handle(first, [second])

        result = await handle.chat_completion(_request())

        assert isinstance(result, ProviderError)
        assert result.code == API_KEY_REJECTED
        assert result.message == sentence
        assert result.detail == ""
        assert result.retryable is False
        assert first.closed is True
        assert first.calls == 1
        assert second.calls == 0
        await handle.close()

    async def test_second_rejection_uses_the_rejected_key_sentence(self) -> None:
        # TD-4840 returned the provider's own ``auth_failed`` after the
        # retry. The window keys its banner off that code, so the host
        # sentence never appeared. The retry now matches an unchanged key.
        first = _Box(OLD, _auth(401, "auth_failed"))
        second = _Box(NEW, _auth(401, "auth_failed"))
        spare = _Box(NEW, _ok_response())
        handle = _handle(first, [second, spare])

        result = await handle.chat_completion(_request())

        assert isinstance(result, ProviderError)
        assert result is not second.outcome
        assert result.code == API_KEY_REJECTED
        assert result.message == EXACT_401
        assert result.detail == ""
        assert result.retryable is False
        assert spare.calls == 0
        assert first.closed is True
        assert second.calls == 1
        await handle.close()

    async def test_second_stream_rejection_uses_the_rejected_key_sentence(self) -> None:
        first = _Box(OLD, _auth(403, "forbidden"))
        second = _Box(NEW, _auth(403, "forbidden"))
        handle = _handle(first, [second])

        items = [item async for item in handle.chat_completion_stream(_request())]

        assert len(items) == 1
        result = items[0]
        assert isinstance(result, ProviderError)
        assert result.code == API_KEY_REJECTED
        assert result.message == EXACT_403
        assert result.detail == ""
        assert second.calls == 1
        assert first.closed is True
        await handle.close()

    async def test_server_error_does_not_rebuild(self) -> None:
        error = ProviderError(code="server_error", message="down", status_code=500, retryable=True)
        first = _Box(OLD, error)

        async def rebuild() -> _Box:
            raise AssertionError("a 500 must not re-read the keychain")

        handle = RefreshingProvider(
            first,
            credential_id="ezer",
            credential_name="EZER",
            base_url=HOST,
            rebuild=rebuild,
        )
        result = await handle.chat_completion(_request())
        assert result is error
        assert first.closed is False
        assert first.calls == 1
        await handle.close()

    async def test_content_already_yielded_is_not_retried(self) -> None:
        error = _auth(401, "auth_failed")
        first = _Box(OLD, [_ok_chunk(), error])

        async def rebuild() -> _Box:
            raise AssertionError("a partial reply must not be retried")

        handle = RefreshingProvider(
            first,
            credential_id="ezer",
            credential_name="EZER",
            base_url=HOST,
            rebuild=rebuild,
        )
        items = [item async for item in handle.chat_completion_stream(_request())]
        assert items == [_ok_chunk(), error]
        assert first.closed is False
        await handle.close()

    async def test_missing_key_on_refresh_is_a_turn_error_then_the_new_key(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="tstd")
        first = _Box(OLD, _auth(401, "auth_failed"))
        stored = _Box(NEW, _ok_chunk())
        phase = {"fail": True}

        async def rebuild() -> _Box:
            if phase["fail"]:
                raise KeychainError(
                    "the stored API key for 'ezer' is empty — re-save it in Settings → API keys"
                )
            return stored

        handle = RefreshingProvider(
            first,
            credential_id="ezer",
            credential_name="EZER",
            base_url=HOST,
            rebuild=rebuild,
        )
        first_pass = [item async for item in handle.chat_completion_stream(_request())]
        assert len(first_pass) == 1
        assert isinstance(first_pass[0], ProviderError)
        assert first_pass[0].code == "missing_api_key"
        assert first.closed is True
        assert stored.calls == 0

        phase["fail"] = False
        second_pass = [item async for item in handle.chat_completion_stream(_request())]
        assert second_pass == [_ok_chunk()]
        assert stored.calls == 1
        _assert_secret_free(_log_blob(caplog), first_pass[0].message)
        await handle.close()


class TestHttpxClose:
    async def _run(
        self,
        *,
        rotated: str,
        statuses: dict[str, int],
    ) -> tuple[
        list[str],
        list[httpx.AsyncClient],
        ChatCompletionResponse | ProviderError,
        RefreshingProvider,
    ]:
        seen: list[str] = []
        clients: list[httpx.AsyncClient] = []
        keys = iter((OLD, rotated))

        def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("authorization", "")
            seen.append(auth)
            token = auth.removeprefix("Bearer ")
            status = statuses.get(token, 401)
            if status == 200:
                return httpx.Response(200, json=OK_BODY)
            return httpx.Response(status, json={"error": {"message": "unauthorized"}})

        def make(api_key: str) -> ProviderClient:
            http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            clients.append(http)
            return ProviderClient(
                base_url=HOST,
                api_key=api_key,
                client=http,
                retry_config=RetryConfig(max_retries=0),
            )

        async def rebuild() -> ProviderClient:
            return make(next(keys))

        handle = RefreshingProvider(
            make(next(keys)),
            credential_id="ezer",
            credential_name="EZER",
            base_url=HOST,
            rebuild=rebuild,
        )
        result = await handle.chat_completion(_request())
        return seen, clients, result, handle

    async def test_rotated_bearer_is_sent_once_and_the_old_client_is_closed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="tstd")
        seen, clients, result, handle = await self._run(rotated=NEW, statuses={OLD: 401, NEW: 200})
        try:
            assert seen == [f"Bearer {OLD}", f"Bearer {NEW}"]
            assert isinstance(result, ChatCompletionResponse)
            assert result.message.content == "ok"
            assert len(clients) == 2
            assert clients[0].is_closed is True
            assert clients[1].is_closed is False
            _assert_secret_free(result.message.content, _log_blob(caplog))
        finally:
            await handle.close()
        assert clients[1].is_closed is True

    async def test_unchanged_bearer_is_not_sent_again(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="tstd")
        # The replacement client is built so the digests can be compared,
        # then left unused until the handle itself is closed.
        seen, clients, result, handle = await self._run(rotated=OLD, statuses={OLD: 401})
        try:
            assert seen == [f"Bearer {OLD}"]
            assert isinstance(result, ProviderError)
            assert result.code == API_KEY_REJECTED
            assert result.message == EXACT_401
            assert result.detail == ""
            assert len(clients) == 2
            assert clients[0].is_closed is True
            assert clients[1].is_closed is False
            _assert_secret_free(result.message, _log_blob(caplog))
        finally:
            await handle.close()
        assert clients[1].is_closed is True

    async def test_forbidden_names_403(self) -> None:
        seen, clients, result, handle = await self._run(rotated=OLD, statuses={OLD: 403})
        try:
            assert seen == [f"Bearer {OLD}"]
            assert isinstance(result, ProviderError)
            assert result.message == EXACT_403
            assert clients[0].is_closed is True
            assert clients[1].is_closed is False
        finally:
            await handle.close()

    async def test_both_keys_rejected_stops_after_one_retry(self) -> None:
        seen, clients, result, handle = await self._run(rotated=NEW, statuses={OLD: 401, NEW: 401})
        try:
            assert seen == [f"Bearer {OLD}", f"Bearer {NEW}"]
            assert isinstance(result, ProviderError)
            assert result.code == API_KEY_REJECTED
            assert result.message == EXACT_401
            assert result.detail == ""
            assert "Authentication failed" not in result.message
            assert len(clients) == 2
            assert clients[0].is_closed is True
            assert clients[1].is_closed is False
            assert OLD not in result.message
            assert NEW not in result.message
        finally:
            await handle.close()

    async def test_server_error_keeps_the_client(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("authorization", ""))
            return httpx.Response(500, json={"error": {"message": "down"}})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        first = ProviderClient(
            base_url=HOST,
            api_key=OLD,
            client=http,
            retry_config=RetryConfig(max_retries=0),
        )

        async def rebuild() -> ProviderClient:
            raise AssertionError("a 500 must not re-read the keychain")

        handle = RefreshingProvider(
            first,
            credential_id="ezer",
            credential_name="EZER",
            base_url=HOST,
            rebuild=rebuild,
        )
        try:
            result = await handle.chat_completion(_request())
            assert isinstance(result, ProviderError)
            assert result.code == "server_error"
            assert seen == [f"Bearer {OLD}"]
            assert http.is_closed is False
        finally:
            await handle.close()
        assert http.is_closed is True


def _ezer_config() -> ModelConfig:
    def tier(slug: str) -> TierConfig:
        return TierConfig(
            slug=slug,
            base_url=HOST,
            credential="ezer",
            input_price=0.0,
            output_price=0.0,
            cache_read_price=0.0,
            context_window=8_192,
            max_output_tokens=1_024,
        )

    return ModelConfig(
        presets={
            "test": Preset(
                brain=tier("ezer-brain"),
                worker=tier("ezer-worker"),
                validator=tier("ezer-validator"),
            )
        },
        active_preset="test",
        credentials={"ezer": CredentialConfig(name="EZER", base_url=HOST)},
    )


def _loopback_config() -> ModelConfig:
    def tier(slug: str) -> TierConfig:
        return TierConfig(
            slug=slug,
            base_url="http://127.0.0.1:11434/v1",
            input_price=0.0,
            output_price=0.0,
            cache_read_price=0.0,
            context_window=8_192,
            max_output_tokens=256,
        )

    return ModelConfig(
        presets={
            "test": Preset(
                brain=tier("local"),
                worker=tier("local-worker"),
                validator=tier("local-validator"),
            )
        },
        active_preset="test",
    )


class KeyedMock(MockProvider):
    """MockProvider that rejects whatever key it was built with, at call time."""

    def __init__(self, api_key: str, reject_status: dict[str, int], *, content: str = "ok") -> None:
        super().__init__(default=Script(kind="stream", content=content))
        self.api_key = api_key
        self.reject_status = reject_status
        self.stream_calls = 0
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    def _rejection(self) -> ProviderError | None:
        status = self.reject_status.get(self.api_key)
        if status is None:
            return None
        if status == 403:
            code = "forbidden"
        elif status == 401:
            code = "auth_failed"
        else:
            code = "server_error"
        return ProviderError(
            code=code,
            message="rejected",
            status_code=status,
            retryable=status in (429, 500, 502, 503, 504),
        )

    async def chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse | ProviderError:
        rejected = self._rejection()
        if rejected is not None:
            return rejected
        return await super().chat_completion(request)

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[StreamChunk | ProviderError]:
        self.stream_calls += 1
        rejected = self._rejection()
        if rejected is not None:
            yield rejected
            return
        async for item in super().chat_completion_stream(request):
            yield item


class _Harness:
    def __init__(
        self,
        daemon: Daemon,
        session: Session,
        runner: SessionRunner,
        built: list[KeyedMock],
        current: dict[str, str | None],
        reject: dict[str, int],
    ) -> None:
        self.daemon = daemon
        self.session = session
        self.runner = runner
        self.built = built
        self.current = current
        self.reject = reject
        self.seen: list[TurnComplete] = []
        self.done = asyncio.Event()

    async def turn(self, text: str = "hi") -> TurnComplete:
        before = len(self.seen)
        self.done.clear()
        await self.session.add_user_message(text)
        try:
            await asyncio.wait_for(self.done.wait(), timeout=5)
        except TimeoutError as exc:
            names = [type(event).__name__ for event in self.session.event_log.all_events]
            raise AssertionError(
                f"turn did not finish (state={self.session.state}, events={names})"
            ) from exc
        assert len(self.seen) == before + 1
        return self.seen[-1]

    async def stop(self) -> None:
        await self.runner.cancel()


async def _start_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    current: dict[str, str | None],
    reject: dict[str, int],
) -> _Harness:
    config = _ezer_config()
    monkeypatch.setattr("tstd.daemon.cached_config", lambda: config)
    daemon = Daemon(data_dir=tmp_path)
    built: list[KeyedMock] = []

    async def build(_tier: TierConfig) -> KeyedMock:
        key = current["key"]
        if key is None or not key.strip():
            raise KeychainError(
                "the stored API key for 'ezer' is empty — re-save it in Settings → API keys"
            )
        mock = KeyedMock(api_key=key, reject_status=reject)
        built.append(mock)
        return mock

    monkeypatch.setattr(daemon, "_build_client", build)
    session = Session(str(tmp_path))
    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            TierRouter(),
            daemon._client_for,
            config,
        ),
    )
    harness = _Harness(daemon, session, runner, built, current, reject)

    async def on_event(event: object, _log: object) -> None:
        if isinstance(event, TurnComplete):
            harness.seen.append(event)
            harness.done.set()

    session.event_log.subscribe(on_event)
    await runner.start()
    return harness


def _assistant(session: Session) -> str:
    return "".join(
        event.delta for event in session.event_log.all_events if isinstance(event, AssistantDelta)
    )


def _assert_window_rejection(session: Session, complete: TurnComplete, sentence: str) -> None:
    """The code and the chat text the window actually renders for this turn."""
    assert complete.failed is True
    assert complete.error_code == API_KEY_REJECTED
    deltas = [
        event.delta for event in session.event_log.all_events if isinstance(event, AssistantDelta)
    ]
    assert deltas[-1] == f"I encountered an error: {sentence}"


class TestDaemonCache:
    async def test_missing_or_empty_key_is_not_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _ezer_config()
        monkeypatch.setattr("tstd.daemon.cached_config", lambda: config)
        daemon = Daemon(data_dir=tmp_path)
        stored: dict[str, str | None] = {}

        async def fake_get(provider_name: str = "openrouter") -> str:
            value = stored.get(provider_name)
            if value is None:
                raise KeychainError(f"API key not found for provider '{provider_name}'.")
            if not value.strip():
                raise KeychainError(
                    f"the stored API key for '{provider_name}' is empty — "
                    "re-save it in Settings → API keys"
                )
            return value

        monkeypatch.setattr("tstd.keychain.get_api_key", fake_get)
        tier = daemon.config.tier("brain")

        with pytest.raises(KeychainError, match="not found"):
            await daemon._client_for(tier)
        assert daemon._clients == {}

        stored["ezer"] = "   "
        with pytest.raises(KeychainError, match="empty"):
            await daemon._client_for(tier)
        assert daemon._clients == {}

        stored["ezer"] = NEW
        handle = await daemon._client_for(tier)
        assert isinstance(handle, RefreshingProvider)
        inner = handle._inner
        assert isinstance(inner, ProviderClient)
        assert inner.api_key == NEW
        http = inner._client
        assert http.is_closed is False
        await handle.close()
        assert http.is_closed is True

    async def test_keyless_client_is_not_wrapped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _loopback_config()
        monkeypatch.setattr("tstd.daemon.cached_config", lambda: config)
        daemon = Daemon(data_dir=tmp_path)
        client = await daemon._client_for(daemon.config.tier("brain"))
        try:
            assert isinstance(client, ProviderClient)
            assert not isinstance(client, RefreshingProvider)
            assert client.api_key is None
            url = "http://127.0.0.1:11434/v1"
            assert daemon._clients[(url, "")] is client
        finally:
            assert isinstance(client, ProviderClient)
            await client.close()

    async def test_in_app_reload_drops_the_cache_without_closing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _ezer_config()
        monkeypatch.setattr("tstd.daemon.cached_config", lambda: config)
        daemon = Daemon(data_dir=tmp_path)
        assert daemon._credential_label("ezer") == "EZER"
        assert daemon._credential_label("openrouter") == "OpenRouter"
        assert daemon._credential_label("other") == "other"
        built: list[KeyedMock] = []
        current = {"key": OLD}

        async def build(_tier: TierConfig) -> KeyedMock:
            key = current["key"]
            assert key is not None
            mock = KeyedMock(api_key=key, reject_status={})
            built.append(mock)
            return mock

        monkeypatch.setattr(daemon, "_build_client", build)
        monkeypatch.setattr("tstd.daemon.load_config", lambda: daemon.config)
        first = await daemon._client_for(daemon.config.tier("brain"))
        assert len(daemon._clients) == 1
        daemon._reload_user_config()
        assert daemon._clients == {}
        assert built[0].closed is False

        current["key"] = NEW
        second = await daemon._client_for(daemon.config.tier("brain"))
        assert second is not first
        assert built[1].api_key == NEW
        assert built[0].closed is False


class TestLoop:
    async def test_stale_key_is_replaced_and_the_retry_succeeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="tstd")
        current: dict[str, str | None] = {"key": OLD}
        reject: dict[str, int] = {}
        harness = await _start_loop(tmp_path, monkeypatch, current=current, reject=reject)
        try:
            first = await harness.turn()
            assert first.failed is False
            assert len(harness.built) == 1
            assert harness.built[0].stream_calls == 1

            current["key"] = NEW
            reject[OLD] = 401
            second = await harness.turn()
            assert second.failed is False
            assert second.error_code is None
            assert len(harness.built) == 2
            assert harness.built[0].closed is True
            assert harness.built[0].stream_calls == 2
            assert harness.built[1].api_key == NEW
            assert harness.built[1].stream_calls == 1

            third = await harness.turn("again")
            assert third.failed is False
            assert len(harness.built) == 2
            assert harness.built[1].stream_calls == 2
            assert "rejected the API key" not in _assistant(harness.session)
            _assert_secret_free(_assistant(harness.session), _log_blob(caplog))
            refresh = [
                record
                for record in caplog.records
                if record.name == "tstd.provider_refresh"
                and record.getMessage() == "provider authentication failed"
            ]
            assert len(refresh) == 1
            extra = refresh[0].extra_fields
            assert extra == {
                "credential": "ezer",
                "host": "192.0.2.49:4000",
                "status_code": 401,
                "key_changed": True,
            }
            assert harness.session.state == "running"
        finally:
            await harness.stop()

    @pytest.mark.parametrize(
        ("status", "sentence"),
        [(401, EXACT_401), (403, EXACT_403)],
    )
    async def test_unchanged_key_fails_the_turn_without_a_retry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        status: int,
        sentence: str,
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="tstd")
        current: dict[str, str | None] = {"key": OLD}
        reject: dict[str, int] = {}
        harness = await _start_loop(tmp_path, monkeypatch, current=current, reject=reject)
        try:
            opened = await harness.turn()
            assert opened.failed is False
            reject[OLD] = status
            failed = await harness.turn()
            _assert_window_rejection(harness.session, failed, sentence)
            shown = _assistant(harness.session)
            assert sentence in shown
            assert sentence in _log_blob(caplog)
            assert "http://" not in shown
            assert "/v1" not in shown
            assert "EZER (" in shown
            assert "ezer (" not in shown
            assert len(harness.built) == 2
            assert harness.built[0].closed is True
            assert harness.built[0].stream_calls == 2
            assert harness.built[1].stream_calls == 0
            assert harness.session.state == "running"
            _assert_secret_free(shown, _log_blob(caplog))
        finally:
            await harness.stop()

    async def test_rotated_key_that_is_also_rejected_is_not_retried_again(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="tstd")
        current: dict[str, str | None] = {"key": OLD}
        reject: dict[str, int] = {}
        harness = await _start_loop(tmp_path, monkeypatch, current=current, reject=reject)
        try:
            await harness.turn()
            current["key"] = NEW
            reject[OLD] = 401
            reject[NEW] = 401
            failed = await harness.turn()
            _assert_window_rejection(harness.session, failed, EXACT_401)
            assert EXACT_401 in _log_blob(caplog)
            assert "Authentication failed" not in _assistant(harness.session)
            assert len(harness.built) == 2
            assert harness.built[0].stream_calls == 2
            assert harness.built[1].stream_calls == 1
            assert harness.session.state == "running"
            _assert_secret_free(_assistant(harness.session), _log_blob(caplog))
        finally:
            await harness.stop()

    async def test_missing_key_caches_nothing_and_the_next_turn_uses_the_new_one(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="tstd")
        current: dict[str, str | None] = {"key": None}
        reject: dict[str, int] = {}
        harness = await _start_loop(tmp_path, monkeypatch, current=current, reject=reject)
        try:
            missing = await harness.turn()
            assert missing.failed is True
            assert missing.error_code == "missing_api_key"
            assert harness.built == []
            assert harness.daemon._clients == {}
            assert harness.session.state == "running"

            current["key"] = NEW
            stored = await harness.turn()
            assert stored.failed is False
            assert len(harness.built) == 1
            assert harness.built[0].api_key == NEW
            assert harness.built[0].stream_calls == 1
            _assert_secret_free(_assistant(harness.session), _log_blob(caplog))
        finally:
            await harness.stop()

    async def test_server_error_does_not_evict(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        current: dict[str, str | None] = {"key": OLD}
        reject: dict[str, int] = {OLD: 500}
        harness = await _start_loop(tmp_path, monkeypatch, current=current, reject=reject)
        try:
            failed = await harness.turn()
            assert failed.failed is True
            assert failed.error_code == "server_error"
            assert len(harness.built) == 1
            assert harness.built[0].closed is False
            assert harness.built[0].stream_calls == 1
            assert len(harness.daemon._clients) == 1
        finally:
            await harness.stop()
