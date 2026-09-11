"""Tests for provider resilience — retry logic, exponential backoff, jitter,
Retry-After, actionable error messages, and partial stream interruption.

All failure modes are exercised against the mock provider (TD-307) and/or
an in-process ASGI mock server so no network is needed.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from tstd.mock import MockProvider, Script
from tstd.provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ProviderClient,
    ProviderError,
    RetryConfig,
    StreamChunk,
    _parse_retry_after,
    auth_failure_message,
    context_length_message,
    retry_call,
    retry_delay,
    retry_stream,
    worst_case_retry_seconds,
)

# ═══════════════════════════════════════════════════════════════════════
#  Unit tests: retry_delay
# ═══════════════════════════════════════════════════════════════════════


class TestWorstCaseRetrySeconds:
    """The bound a waiting client uses to outlast the daemon's retries."""

    def test_sums_every_delay(self) -> None:
        cfg = RetryConfig(initial_delay=1.0, max_delay=60.0, jitter_factor=0.0, max_retries=4)
        # 1 + 2 + 4 + 8
        assert worst_case_retry_seconds(cfg) == 15.0

    def test_respects_the_cap(self) -> None:
        cfg = RetryConfig(initial_delay=1.0, max_delay=5.0, jitter_factor=0.0, max_retries=5)
        # 1 + 2 + 4 + 5 + 5
        assert worst_case_retry_seconds(cfg) == 17.0

    def test_includes_jitter_headroom(self) -> None:
        """Jitter can only extend a wait, so the bound has to carry it."""
        plain = RetryConfig(initial_delay=1.0, jitter_factor=0.0, max_retries=3)
        jittered = RetryConfig(initial_delay=1.0, jitter_factor=0.25, max_retries=3)
        assert worst_case_retry_seconds(jittered) > worst_case_retry_seconds(plain)

    def test_no_retries_costs_nothing(self) -> None:
        assert worst_case_retry_seconds(RetryConfig(max_retries=0)) == 0.0

    def test_bound_actually_covers_real_delays(self) -> None:
        """The bound must not undercut what retry_delay can produce."""
        cfg = RetryConfig(initial_delay=1.0, max_delay=8.0, jitter_factor=0.25, max_retries=5)
        for _ in range(50):
            drawn = sum(retry_delay(a, cfg) for a in range(1, cfg.max_retries + 1))
            assert drawn <= worst_case_retry_seconds(cfg) + 1e-9


class TestRetryDelay:
    def test_exponential_backoff(self) -> None:
        """Delay doubles each attempt (no jitter)."""
        cfg = RetryConfig(initial_delay=1.0, max_delay=60.0, jitter_factor=0.0)
        assert retry_delay(1, cfg) == 1.0
        assert retry_delay(2, cfg) == 2.0
        assert retry_delay(3, cfg) == 4.0
        assert retry_delay(4, cfg) == 8.0

    def test_capped_at_max_delay(self) -> None:
        """Delay is capped at max_delay."""
        cfg = RetryConfig(initial_delay=1.0, max_delay=5.0, jitter_factor=0.0)
        assert retry_delay(10, cfg) == 5.0

    def test_jitter_range(self) -> None:
        """Delay is within [base, base*(1+jitter)) when jitter_factor > 0."""
        cfg = RetryConfig(initial_delay=10.0, jitter_factor=0.5)
        for _ in range(20):
            d = retry_delay(1, cfg)
            assert 10.0 <= d < 15.0, f"delay {d} out of range"

    def test_retry_after_honoured(self) -> None:
        """When retry_after > base, delay is at least retry_after."""
        cfg = RetryConfig(initial_delay=0.5, max_delay=10.0, jitter_factor=0.0)
        # retry_after=3.0 > 0.5, so delay should be 3.0 (no jitter)
        assert retry_delay(1, cfg, retry_after=3.0) == 3.0

    def test_retry_after_shorter_than_backoff(self) -> None:
        """When retry_after < base, delay uses the backoff."""
        cfg = RetryConfig(initial_delay=5.0, jitter_factor=0.0)
        # base=5.0 > retry_after=1.0, so delay=5.0
        assert retry_delay(1, cfg, retry_after=1.0) == 5.0


# ═══════════════════════════════════════════════════════════════════════
#  Unit tests: _parse_retry_after
# ═══════════════════════════════════════════════════════════════════════


class TestParseRetryAfter:
    def test_none(self) -> None:
        assert _parse_retry_after(None) is None

    def test_seconds_integer(self) -> None:
        assert _parse_retry_after("120") == 120.0

    def test_seconds_decimal(self) -> None:
        result = _parse_retry_after("2.5")
        assert result is not None
        assert abs(result - 2.5) < 0.001

    def test_http_date(self) -> None:
        """HTTP-date format: future date gives positive seconds."""
        # Use a date far in the future so it's always "after now"
        result = _parse_retry_after("Wed, 12 Aug 2030 18:00:00 GMT")
        assert result is not None
        assert result > 0

    def test_http_date_past(self) -> None:
        """Past date returns 0.0 (clamped)."""
        result = _parse_retry_after("Mon, 1 Jan 2020 00:00:00 GMT")
        assert result is not None
        assert result == 0.0

    def test_garbage(self) -> None:
        assert _parse_retry_after("not-a-date") is None


# ═══════════════════════════════════════════════════════════════════════
#  Unit tests: actionable error messages
# ═══════════════════════════════════════════════════════════════════════


class TestActionableMessages:
    def test_auth_message(self) -> None:
        msg = auth_failure_message()
        assert "API key" in msg
        assert "title bar" in msg  # TD-1102: points at the wizard, not a phantom CLI
        assert "base_url" in msg

    def test_context_length_message(self) -> None:
        msg = context_length_message()
        assert "context window" in msg
        assert "reduce" in msg.lower()

    def test_context_length_message_with_detail(self) -> None:
        msg = context_length_message("maximum 128000 tokens")
        assert "128000" in msg


# ═══════════════════════════════════════════════════════════════════════
#  Unit tests: RetryConfig validation
# ═══════════════════════════════════════════════════════════════════════


class TestRetryConfig:
    def test_defaults(self) -> None:
        cfg = RetryConfig()
        assert cfg.max_retries == 3
        assert cfg.initial_delay == 1.0
        assert cfg.max_delay == 60.0
        assert cfg.jitter_factor == 0.25

    def test_negative_max_retries(self) -> None:
        with pytest.raises(ValueError, match="max_retries"):
            RetryConfig(max_retries=-1)

    def test_zero_initial_delay(self) -> None:
        with pytest.raises(ValueError, match="initial_delay"):
            RetryConfig(initial_delay=0)

    def test_negative_jitter(self) -> None:
        with pytest.raises(ValueError, match="jitter_factor"):
            RetryConfig(jitter_factor=-0.1)

    def test_jitter_too_high(self) -> None:
        with pytest.raises(ValueError, match="jitter_factor"):
            RetryConfig(jitter_factor=1.1)


# ═══════════════════════════════════════════════════════════════════════
#  retry_call with MockProvider
# ═══════════════════════════════════════════════════════════════════════


class TestRetryCall:
    """Tests retry_call() using the MockProvider for deterministic failures."""

    @pytest.fixture
    def mock(self) -> MockProvider:
        return MockProvider()

    @pytest.fixture
    def req(self) -> ChatCompletionRequest:
        return ChatCompletionRequest(
            model="test", messages=[ChatMessage(role="user", content="Hi")]
        )

    async def test_retry_on_rate_limit_then_succeed(
        self, mock: MockProvider, req: ChatCompletionRequest
    ) -> None:
        """rate_limit twice, then succeed."""
        mock.script(
            "test",
            Script(kind="text", content="Success!", fail_times=2),
        )
        result = await retry_call(
            lambda: mock.chat_completion(req),
            RetryConfig(max_retries=3, initial_delay=0.01, jitter_factor=0.0),
        )
        assert isinstance(result, ChatCompletionResponse)
        assert result.message.content == "Success!"
        # 2 failures + 1 success = 3 calls
        assert len(mock.calls) == 3

    async def test_retry_on_server_error_then_succeed(
        self, mock: MockProvider, req: ChatCompletionRequest
    ) -> None:
        """server_error once, then succeed."""
        mock.script(
            "test",
            Script(
                kind="text",
                content="OK",
                fail_times=1,
                status_code=500,
                error_code="server_error",
            ),
        )
        result = await retry_call(
            lambda: mock.chat_completion(req),
            RetryConfig(max_retries=3, initial_delay=0.01, jitter_factor=0.0),
        )
        assert isinstance(result, ChatCompletionResponse)
        assert result.message.content == "OK"
        assert len(mock.calls) == 2

    async def test_bounded_retries_exhausted(
        self, mock: MockProvider, req: ChatCompletionRequest
    ) -> None:
        """fail_times > max_retries → returns error after exhaustion."""
        mock.script(
            "test",
            Script(kind="rate_limit", fail_times=10),
        )
        result = await retry_call(
            lambda: mock.chat_completion(req),
            RetryConfig(max_retries=2, initial_delay=0.01, jitter_factor=0.0),
        )
        assert isinstance(result, ProviderError)
        assert result.code == "rate_limited"
        # 1 initial + 2 retries = 3 calls
        assert len(mock.calls) == 3

    async def test_no_retry_on_non_retryable(
        self, mock: MockProvider, req: ChatCompletionRequest
    ) -> None:
        """Non-retryable errors (auth, parse, context-length) never retry."""
        mock.script(
            "test",
            Script(kind="error", status_code=401, error_code="auth_failed"),
        )
        result = await retry_call(
            lambda: mock.chat_completion(req),
            RetryConfig(max_retries=3, initial_delay=0.01, jitter_factor=0.0),
        )
        assert isinstance(result, ProviderError)
        assert result.code == "auth_failed"
        assert len(mock.calls) == 1

    async def test_no_retry_on_parse_error(
        self, mock: MockProvider, req: ChatCompletionRequest
    ) -> None:
        mock.script("test", Script(kind="malformed"))
        result = await retry_call(
            lambda: mock.chat_completion(req),
            RetryConfig(max_retries=3, initial_delay=0.01, jitter_factor=0.0),
        )
        assert isinstance(result, ProviderError)
        assert result.code == "parse_error"
        assert len(mock.calls) == 1

    async def test_retry_after_delay(self, mock: MockProvider, req: ChatCompletionRequest) -> None:
        """Retry-After honoured: delay is at least retry_after seconds."""
        mock.script(
            "test",
            Script(
                kind="text",
                content="OK",
                fail_times=1,
                retry_after=0.05,
            ),
        )
        cfg = RetryConfig(max_retries=1, initial_delay=0.01, jitter_factor=0.0)
        start = time.monotonic()
        result = await retry_call(lambda: mock.chat_completion(req), cfg)
        elapsed = time.monotonic() - start
        assert isinstance(result, ChatCompletionResponse)
        assert result.message.content == "OK"
        # With jitter_factor=0, delay = max(0.01, 0.05) = 0.05
        assert elapsed >= 0.045

    async def test_zero_max_retries_no_retry(
        self, mock: MockProvider, req: ChatCompletionRequest
    ) -> None:
        """max_retries=0 means no retry."""
        mock.script("test", Script(kind="rate_limit"))
        result = await retry_call(
            lambda: mock.chat_completion(req),
            RetryConfig(max_retries=0, initial_delay=0.01, jitter_factor=0.0),
        )
        assert isinstance(result, ProviderError)
        assert result.code == "rate_limited"
        assert len(mock.calls) == 1

    async def test_no_delay_for_success(
        self, mock: MockProvider, req: ChatCompletionRequest
    ) -> None:
        """Successful response returns immediately without delay."""
        mock.script("test", Script(kind="text", content="Instant"))
        start = time.monotonic()
        result = await retry_call(
            lambda: mock.chat_completion(req),
            RetryConfig(max_retries=3, initial_delay=5.0, jitter_factor=0.0),
        )
        elapsed = time.monotonic() - start
        assert isinstance(result, ChatCompletionResponse)
        assert elapsed < 1.0  # far less than 5s


# ═══════════════════════════════════════════════════════════════════════
#  retry_stream with MockProvider
# ═══════════════════════════════════════════════════════════════════════


class TestRetryStream:
    """Tests retry_stream() using the MockProvider."""

    @pytest.fixture
    def mock(self) -> MockProvider:
        return MockProvider()

    @pytest.fixture
    def req(self) -> ChatCompletionRequest:
        return ChatCompletionRequest(
            model="test", messages=[ChatMessage(role="user", content="Hi")]
        )

    async def _collect(
        self, stream: MockProvider, req: ChatCompletionRequest, cfg: RetryConfig
    ) -> list[StreamChunk | ProviderError]:
        items: list[StreamChunk | ProviderError] = []
        async for item in retry_stream(lambda: stream.chat_completion_stream(req), cfg):
            items.append(item)
        return items

    async def test_retry_initial_rate_limit_then_stream(
        self, mock: MockProvider, req: ChatCompletionRequest
    ) -> None:
        """Fail once on initial connect, then stream succeeds."""
        mock.script(
            "test",
            Script(kind="stream", content="Retried stream", fail_times=1),
        )
        items = await self._collect(
            mock, req, RetryConfig(max_retries=3, initial_delay=0.01, jitter_factor=0.0)
        )
        # Should have streamed content chunks
        texts = [
            i.delta.content or "" for i in items if isinstance(i, StreamChunk) and i.delta.content
        ]
        assert "".join(texts) == "Retried stream"
        # 1 fail + 1 success = 2 calls
        assert len(mock.calls) == 2

    async def test_bounded_retry_on_stream(
        self, mock: MockProvider, req: ChatCompletionRequest
    ) -> None:
        """fail_times > max_retries → returns error."""
        mock.script(
            "test",
            Script(kind="stream", content="X", fail_times=10),
        )
        items = await self._collect(
            mock, req, RetryConfig(max_retries=2, initial_delay=0.01, jitter_factor=0.0)
        )
        assert len(items) == 1
        assert isinstance(items[0], ProviderError)
        assert items[0].code == "rate_limited"
        # 1 initial + 2 retries = 3 calls
        assert len(mock.calls) == 3

    # Note: "no retry after first content chunk" is tested at the ASGI level
    # in TestStreamInterruption and TestStreamDrop — the ProviderClient's
    # _stream_once detects mid-stream interruption and produces a
    # stream_interrupted error without retrying.


# ═══════════════════════════════════════════════════════════════════════
#  Stream interruption (via ASGI mock server)
# ═══════════════════════════════════════════════════════════════════════


class _StreamInterruptASGI:
    """ASGI app that streams tool-call chunks then closes without finish_reason."""

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert scope["type"] == "http"

        # Consume request body
        body_bytes = b""
        more_body = True
        while more_body:
            msg = await receive()
            if msg["type"] == "http.request":
                body_bytes += msg.get("body", b"")
                more_body = msg.get("more_body", False)

        req_body = json.loads(body_bytes) if body_bytes else {}
        model = req_body.get("model", "mock-model")

        # Interrupt model: stream tool-call chunks then close without finish
        if model == "interrupt-tool":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        [b"content-type", b"text/event-stream"],
                        [b"cache-control", b"no-cache"],
                    ],
                }
            )
            # Chunk 1: tool call id + name
            chunk1 = {
                "id": "mock-stream-1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_abc123",
                                    "type": "function",
                                    "function": {"name": "get_weather", "arguments": ""},
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            }
            await send(
                {
                    "type": "http.response.body",
                    "body": f"data: {json.dumps(chunk1)}\n\n".encode(),
                    "more_body": True,
                }
            )
            # Chunk 2: partial arguments
            chunk2 = {
                "id": "mock-stream-1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"location": "Sa'},
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            }
            await send(
                {
                    "type": "http.response.body",
                    "body": f"data: {json.dumps(chunk2)}\n\n".encode(),
                    "more_body": False,  # ← clean close, no finish chunk
                }
            )
            return

        # Normal model: plain text stream
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    [b"content-type", b"text/event-stream"],
                    [b"cache-control", b"no-cache"],
                ],
            }
        )
        for word in ["Hello", ", ", "world!"]:
            chunk = {
                "id": "mock-stream-1",
                "choices": [{"index": 0, "delta": {"content": word}, "finish_reason": None}],
            }
            await send(
                {
                    "type": "http.response.body",
                    "body": f"data: {json.dumps(chunk)}\n\n".encode(),
                    "more_body": True,
                }
            )
        final = {
            "id": "mock-stream-1",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        }
        await send(
            {
                "type": "http.response.body",
                "body": f"data: {json.dumps(final)}\n\n".encode(),
                "more_body": False,
            }
        )


@pytest.fixture
def interrupt_client() -> ProviderClient:
    transport = httpx.ASGITransport(app=_StreamInterruptASGI())  # type: ignore[arg-type]
    return ProviderClient(
        base_url="http://mock/v1",
        api_key="sk-test-key",
        client=httpx.AsyncClient(transport=transport),
        retry_config=RetryConfig(max_retries=0),  # single-shot for this test
    )


class TestStreamInterruption:
    """ "ProviderClient.stream_once" interruption detection via ASGI.

    Tests that the ProviderClient detects an in-flight tool call when the
    stream ends without a finish_reason and yields a stream_interrupted
    error — so the consumer never dispatches a half-parsed tool call.
    """

    async def test_clean_close_mid_tool_call(self, interrupt_client: ProviderClient) -> None:
        """Stream ends cleanly mid-tool-call → stream_interrupted error."""
        request = ChatCompletionRequest(
            model="interrupt-tool",
            messages=[ChatMessage(role="user", content="Hi")],
        )
        items: list[StreamChunk | ProviderError] = []
        async for chunk in interrupt_client.chat_completion_stream(request):
            items.append(chunk)

        # Should have tool-call deltas
        tool_chunks = [i for i in items if isinstance(i, StreamChunk) and i.delta.tool_calls]
        assert len(tool_chunks) >= 1

        # Should end with a stream_interrupted ProviderError
        errors = [i for i in items if isinstance(i, ProviderError)]
        assert len(errors) == 1
        assert errors[0].code == "stream_interrupted"

    async def test_normal_stream_no_interruption(self, interrupt_client: ProviderClient) -> None:
        """Normal stream with finish_reason does NOT produce an error."""
        request = ChatCompletionRequest(
            model="normal",
            messages=[ChatMessage(role="user", content="Hi")],
        )
        items: list[StreamChunk | ProviderError] = []
        async for chunk in interrupt_client.chat_completion_stream(request):
            items.append(chunk)

        # Should have text content
        texts = [i.delta.content or "" for i in items if isinstance(i, StreamChunk)]
        assert "".join(texts) == "Hello, world!"

        # No errors
        errors = [i for i in items if isinstance(i, ProviderError)]
        assert len(errors) == 0


# ═══════════════════════════════════════════════════════════════════════
#  Stream drop (connection exception mid-stream)
# ═══════════════════════════════════════════════════════════════════════


class _DroppingStream(httpx.AsyncByteStream):
    """Yields N SSE chunks then raises httpx.ReadError to simulate a drop."""

    def __init__(self, chunks: list[bytes], drop_after: int) -> None:
        self._chunks = list(chunks)
        self._drop_after = drop_after

    async def __aiter__(self):
        for i, chunk in enumerate(self._chunks):
            if i >= self._drop_after:
                raise httpx.ReadError("connection dropped mid-stream", request=None)
            yield chunk

    async def aclose(self) -> None:
        pass


class _DroppingTransport(httpx.AsyncBaseTransport):
    """Transport that returns a response body whose stream drops mid-way."""

    def __init__(self, chunks: list[bytes], drop_after: int) -> None:
        self._chunks = chunks
        self._drop_after = drop_after

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_DroppingStream(self._chunks, self._drop_after),
            request=request,
        )


def _sse_chunk(data: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(data)}\n\n".encode()


@pytest.fixture
def drop_client() -> ProviderClient:
    """Client whose transport drops mid-stream after 2 tool-call SSE chunks."""
    chunk1 = _sse_chunk(
        {
            "id": "mock-stream-1",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_abc",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": ""},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        }
    )
    chunk2 = _sse_chunk(
        {
            "id": "mock-stream-1",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '{"loc": "CA"}'},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        }
    )
    transport = _DroppingTransport([chunk1, chunk2], drop_after=2)
    return ProviderClient(
        base_url="http://mock/v1",
        api_key="sk-test-key",
        client=httpx.AsyncClient(transport=transport),
        retry_config=RetryConfig(max_retries=0),  # single-shot
    )


class TestStreamDrop:
    """Connection drop mid-stream via httpx.ReadError."""

    async def test_drop_mid_tool_call(self, drop_client: ProviderClient) -> None:
        """Transport raises ReadError mid-tool-call → stream_interrupted."""
        request = ChatCompletionRequest(
            model="test",
            messages=[ChatMessage(role="user", content="Hi")],
        )
        items: list[StreamChunk | ProviderError] = []
        async for chunk in drop_client.chat_completion_stream(request):
            items.append(chunk)

        # Should have tool-call deltas before the drop
        tool_chunks = [i for i in items if isinstance(i, StreamChunk) and i.delta.tool_calls]
        assert len(tool_chunks) >= 1

        # The last item should be a stream_interrupted error
        last = items[-1]
        assert isinstance(last, ProviderError)
        assert last.code == "stream_interrupted"
        assert "discarded" in last.message


# ═══════════════════════════════════════════════════════════════════════
#  Retry-After header parsing (via ASGI)
# ═══════════════════════════════════════════════════════════════════════


class _RetryAfterASGI:
    """ASGI app that returns 429 with a Retry-After header."""

    def __init__(self) -> None:
        self.call_count = 0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        self.call_count += 1
        # Consume body
        body_bytes = b""
        more_body = True
        while more_body:
            msg = await receive()
            if msg["type"] == "http.request":
                body_bytes += msg.get("body", b"")
                more_body = msg.get("more_body", False)

        # Always return 429
        data = {"error": {"message": "Rate limited", "code": "rate_limited"}}
        body = json.dumps(data).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"retry-after", b"5"],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class TestRetryAfterHeader:
    """Retry-After header is parsed and returned in the ProviderError."""

    async def test_retry_after_parsed(self) -> None:
        """ProviderError.retry_after is set from the Retry-After header."""
        app = _RetryAfterASGI()
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        client = ProviderClient(
            base_url="http://mock/v1",
            api_key="sk-test-key",
            client=httpx.AsyncClient(transport=transport),
            retry_config=RetryConfig(max_retries=0),
        )
        request = ChatCompletionRequest(
            model="test",
            messages=[ChatMessage(role="user", content="Hi")],
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ProviderError)
        assert result.code == "rate_limited"
        assert result.retry_after == 5.0


# ═══════════════════════════════════════════════════════════════════════
#  Reconnection retry on stream (integration)
# ═══════════════════════════════════════════════════════════════════════


class _FlakyConnectASGI:
    """Returns 429 on first call, then streams normally."""

    def __init__(self) -> None:
        self.call_count = 0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        self.call_count += 1
        # Consume body
        body_bytes = b""
        more_body = True
        while more_body:
            msg = await receive()
            if msg["type"] == "http.request":
                body_bytes += msg.get("body", b"")
                more_body = msg.get("more_body", False)

        if self.call_count == 1:
            # First call fails with 429
            data = {"error": {"message": "Rate limited", "code": "rate_limited"}}
            body = json.dumps(data).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [[b"content-type", b"application/json"]],
                }
            )
            await send({"type": "http.response.body", "body": body})
        else:
            # Second call streams normally
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        [b"content-type", b"text/event-stream"],
                        [b"cache-control", b"no-cache"],
                    ],
                }
            )
            chunk = {
                "id": "mock-stream-1",
                "choices": [{"index": 0, "delta": {"content": "Retried!"}, "finish_reason": None}],
            }
            await send(
                {
                    "type": "http.response.body",
                    "body": f"data: {json.dumps(chunk)}\n\n".encode(),
                    "more_body": True,
                }
            )
            final = {
                "id": "mock-stream-1",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
            }
            await send(
                {
                    "type": "http.response.body",
                    "body": f"data: {json.dumps(final)}\n\n".encode(),
                    "more_body": False,
                }
            )


class TestStreamReconnect:
    """Stream that retries on initial connection failure."""

    async def test_retry_on_connect_then_stream(self) -> None:
        """Stream retries on 429 initial response, then succeeds."""
        app = _FlakyConnectASGI()
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        client = ProviderClient(
            base_url="http://mock/v1",
            api_key="sk-test-key",
            client=httpx.AsyncClient(transport=transport),
            retry_config=RetryConfig(max_retries=2, initial_delay=0.01, jitter_factor=0.0),
        )
        request = ChatCompletionRequest(
            model="test",
            messages=[ChatMessage(role="user", content="Hi")],
        )
        texts: list[str] = []
        errors: list[ProviderError] = []
        async for chunk in client.chat_completion_stream(request):
            if isinstance(chunk, StreamChunk) and chunk.delta.content:
                texts.append(chunk.delta.content)
            elif isinstance(chunk, ProviderError):
                errors.append(chunk)

        assert "".join(texts) == "Retried!"
        assert len(errors) == 0
        assert app.call_count == 2
