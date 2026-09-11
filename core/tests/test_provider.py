"""Tests for the provider client — request building, response parsing,
streaming, tool calling, error handling, and timeouts.

Uses an in-process ASGI mock server so no network is needed.
"""

from __future__ import annotations

import json
import socket
import sys
from typing import Any

import httpx
import pytest

from tstd.provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    DeltaToolCall,
    FunctionCall,
    FunctionDefinition,
    ProviderClient,
    ProviderError,
    RetryConfig,
    StreamChunk,
    TimeoutConfig,
    ToolCall,
    ToolDefinition,
    Usage,
)

# ── Mock ASGI server ───────────────────────────────────────────────────


async def _mock_chat_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """ASGI app that serves chat completions with scripted responses.

    Behavior is controlled by the request body:
    - ``stream``: return SSE stream or single response
    - Tool calls returned when the model name starts with ``tool-``
    - Error codes returned when the model name starts with ``err-``
    """
    assert scope["type"] == "http"
    path = scope["path"]

    # Accept any path ending in /chat/completions (works with or without /v1 prefix)
    if not path.endswith("/chat/completions"):
        data = {"error": {"message": "Not found"}}
        await _send_response(send, 404, data)
        return

    # Read the request body
    body_bytes = b""
    more_body = True
    while more_body:
        msg = await receive()
        if msg["type"] == "http.request":
            body_bytes += msg.get("body", b"")
            more_body = msg.get("more_body", False)

    req_body = json.loads(body_bytes) if body_bytes else {}
    model = req_body.get("model", "mock-model")
    stream = req_body.get("stream", False)

    # Error simulation, provider echoes a numeric code: "errc-{status_code}".
    # This is the shape OpenRouter actually sends — {"code": 429} beside the
    # 429 — and the plain "err-" body below, which omits it, is what let a
    # numeric code shadow the status map unnoticed.
    if model.startswith("errc-"):
        status = int(model.split("-")[1])
        data = {"error": {"message": f"Simulated error: {status}", "code": status}}
        await _send_response(send, status, data)
        return

    # Error simulation with a gateway placeholder message and the real reason
    # in metadata: "errm-{status_code}". OpenRouter answers an upstream
    # capacity refusal with the literal "Provider returned error" and puts the
    # cause and the way out in metadata.raw / metadata.remedy_hint.
    if model.startswith("errm-"):
        status = int(model.split("-")[1])
        data = {
            "error": {
                "message": "Provider returned error",
                "code": status,
                "metadata": {
                    "raw": "stealth/sim-model is temporarily rate-limited upstream. "
                    "Please retry shortly.",
                    "provider_name": "Stealth",
                    "limit_source": "upstream_provider_shared_pool",
                    "remedy_hint": "Retry shortly, or route to another provider.",
                },
            }
        }
        await _send_response(send, status, data)
        return

    # Error simulation: model name "err-{status_code}"
    if model.startswith("err-"):
        status = int(model.split("-")[1])
        data = {"error": {"message": f"Simulated error: {status}"}}
        await _send_response(send, status, data)
        return

    # A 200 event-stream that carries no events at all: "empty-stream".
    # Observed live from OpenRouter on a throttled model — the request is
    # accepted, the stream opens, and it closes without a single chunk.
    if model == "empty-stream":
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
        await send({"type": "http.response.body", "body": b""})
        return

    # An error envelope delivered under HTTP 200: "ok-err-{code}". Observed
    # live from OpenRouter — {"error": {"code": 429}} with a 200 status line
    # and no "choices" — on both the streaming and non-streaming routes.
    if model.startswith("ok-err-"):
        code = int(model.rsplit("-", 1)[1])
        await _send_response(
            send, 200, {"error": {"message": "Provider returned error", "code": code}}
        )
        return

    # Tool call simulation: model name "tool-{model}"
    is_tool = model.startswith("tool-")

    if stream:
        await _send_streaming_response(send, req_body, is_tool)
    else:
        await _send_nonstreaming_response(send, req_body, is_tool)


async def _send_response(send: Any, status: int, data: dict[str, Any]) -> None:
    body = json.dumps(data).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [[b"content-type", b"application/json"]],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": body,
        }
    )


async def _send_nonstreaming_response(send: Any, req_body: dict[str, Any], tool: bool) -> None:
    model = req_body.get("model", "mock-model")
    if tool:
        data = {
            "id": "mock-call-1",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc123",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"location": "San Francisco"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        }
    else:
        data = {
            "id": "mock-1",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Hello, world!",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    await _send_response(send, 200, data)


async def _send_streaming_response(send: Any, req_body: dict[str, Any], tool: bool) -> None:
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

    if tool:
        # First chunk: role + tool call id
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
        # Second chunk: arguments
        chunk2 = {
            "id": "mock-stream-1",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '{"location": "San Francisco"}'},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        }
        # Final chunk with finish reason and usage
        chunk3 = {
            "id": "mock-stream-1",
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        }
        chunks = [chunk1, chunk2, chunk3]
    else:
        chunk1 = {
            "id": "mock-stream-1",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "Hello"},
                    "finish_reason": None,
                }
            ],
        }
        chunk2 = {
            "id": "mock-stream-1",
            "choices": [{"index": 0, "delta": {"content": ", "}, "finish_reason": None}],
        }
        chunk3 = {
            "id": "mock-stream-1",
            "choices": [{"index": 0, "delta": {"content": "world!"}, "finish_reason": None}],
        }
        chunk4 = {
            "id": "mock-stream-1",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        chunks = [chunk1, chunk2, chunk3, chunk4]

    for chunk in chunks:
        line = f"data: {json.dumps(chunk)}\n\n"
        await send(
            {
                "type": "http.response.body",
                "body": line.encode(),
                "more_body": True,
            }
        )

    # Send the terminal signal
    await send(
        {
            "type": "http.response.body",
            "body": b"data: [DONE]\n\n",
            "more_body": False,
        }
    )


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def mock_transport() -> httpx.ASGITransport:
    return httpx.ASGITransport(app=_mock_chat_app)  # type: ignore[arg-type]


@pytest.fixture
def client(mock_transport: httpx.ASGITransport) -> ProviderClient:
    # Retry disabled: error-handling tests assert single-shot behaviour.
    return ProviderClient(
        base_url="http://mock/v1",
        api_key="sk-test-key",
        client=httpx.AsyncClient(transport=mock_transport),
        retry_config=RetryConfig(max_retries=0),
    )


@pytest.fixture
def basic_request() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="mock-model",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Say hello"),
        ],
    )


# ── Tests: Non-streaming ───────────────────────────────────────────────


class TestNonStreaming:
    async def test_basic_chat_completion(
        self, client: ProviderClient, basic_request: ChatCompletionRequest
    ) -> None:
        result = await client.chat_completion(basic_request)
        assert isinstance(result, ChatCompletionResponse), f"Expected response, got error: {result}"
        assert result.message.content == "Hello, world!"
        assert result.finish_reason == "stop"
        assert result.usage is not None
        assert result.usage.prompt_tokens == 10
        assert result.usage.completion_tokens == 5
        assert result.usage.total_tokens == 15

    async def test_tool_calling_response(self, client: ProviderClient) -> None:
        """Test parsing a response with tool calls."""
        request = ChatCompletionRequest(
            model="tool-test",
            messages=[ChatMessage(role="user", content="What's the weather?")],
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ChatCompletionResponse), f"Expected response, got error: {result}"
        assert result.message.tool_calls is not None
        assert len(result.message.tool_calls) == 1
        assert result.message.tool_calls is not None
        fn = result.message.tool_calls[0].function
        assert fn is not None
        assert fn.name == "get_weather"
        assert fn.arguments == '{"location": "San Francisco"}'
        assert result.finish_reason == "tool_calls"

    async def test_request_serialization(self) -> None:
        """Test that the request is serialized correctly."""
        request = ChatCompletionRequest(
            model="test-model",
            messages=[
                ChatMessage(role="user", content="Hi"),
            ],
            tools=[
                ToolDefinition(
                    function=FunctionDefinition(
                        name="test_func",
                        description="A test function",
                        parameters={"type": "object", "properties": {"x": {"type": "string"}}},
                    )
                )
            ],
            max_tokens=100,
            temperature=0.5,
            top_p=0.9,
            stop=["\n"],
        )
        d = request.to_dict()
        assert d["model"] == "test-model"
        assert d["messages"] == [{"role": "user", "content": "Hi"}]
        assert d["tools"][0]["function"]["name"] == "test_func"
        assert d["max_tokens"] == 100
        assert d["temperature"] == 0.5
        assert d["top_p"] == 0.9
        assert d["stop"] == ["\n"]

    async def test_extra_body(self) -> None:
        """Test that extra_body fields are merged into the request."""
        request = ChatCompletionRequest(
            model="test-model",
            messages=[ChatMessage(role="user", content="Hello")],
            extra_body={"provider": {"order": ["DeepSeek"]}},
        )
        d = request.to_dict()
        assert d["provider"]["order"] == ["DeepSeek"]

    async def test_message_with_tool_calls_in_request(self) -> None:
        """Test serializing a message with tool results."""
        request = ChatCompletionRequest(
            model="test-model",
            messages=[
                ChatMessage(role="user", content="What's the weather?"),
                ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="call_prev",
                            function=FunctionCall(
                                name="get_weather",
                                arguments='{"location": "SF"}',
                            ),
                        )
                    ],
                ),
                ChatMessage(
                    role="tool",
                    content="Sunny, 72°F",
                    tool_call_id="call_prev",
                ),
            ],
        )
        d = request.to_dict()
        assert len(d["messages"]) == 3
        assert d["messages"][1]["tool_calls"][0]["function"]["name"] == "get_weather"
        assert d["messages"][2]["tool_call_id"] == "call_prev"


# ── Tests: Streaming ───────────────────────────────────────────────────


class TestStreaming:
    async def test_basic_stream(
        self, client: ProviderClient, basic_request: ChatCompletionRequest
    ) -> None:
        """Test basic streaming response with text deltas."""
        chunks: list[StreamChunk] = []
        async for chunk in client.chat_completion_stream(basic_request):
            if isinstance(chunk, StreamChunk):
                chunks.append(chunk)

        # We expect 3 content chunks + 1 final chunk with usage
        texts = []
        for c in chunks:
            if c.delta.content:
                texts.append(c.delta.content)
        assert "".join(texts) == "Hello, world!"

        # The last chunk should have finish_reason and usage
        final = chunks[-1]
        assert final.finish_reason == "stop"
        assert final.usage is not None
        assert final.usage.prompt_tokens == 10
        assert final.usage.completion_tokens == 5

    async def test_stream_with_tool_calls(self, client: ProviderClient) -> None:
        """Test streaming response with tool call deltas."""
        request = ChatCompletionRequest(
            model="tool-test",
            messages=[ChatMessage(role="user", content="What's the weather?")],
        )
        chunks: list[StreamChunk] = []
        async for chunk in client.chat_completion_stream(request):
            if isinstance(chunk, StreamChunk):
                chunks.append(chunk)

        # Should have tool call deltas
        tool_call_chunks = [c for c in chunks if c.delta.tool_calls]
        assert len(tool_call_chunks) >= 1

        # The first tool call chunk should have the id and function name
        first_tcs = tool_call_chunks[0].delta.tool_calls
        assert first_tcs is not None
        first_tc = first_tcs[0]
        assert first_tc.id == "call_abc123"
        assert first_tc.function_name == "get_weather"

        # Last chunk should have finish_reason = tool_calls
        assert chunks[-1].finish_reason == "tool_calls"


# ── Tests: Error handling ──────────────────────────────────────────────


class TestErrorHandling:
    async def test_auth_failure(self, client: ProviderClient) -> None:
        """Test that 401 is handled correctly."""
        request = ChatCompletionRequest(
            model="err-401", messages=[ChatMessage(role="user", content="Hi")]
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ProviderError)
        assert result.code == "auth_failed"
        assert result.status_code == 401
        assert not result.retryable

    async def test_rate_limit(self, client: ProviderClient) -> None:
        """Test that 429 is retryable."""
        request = ChatCompletionRequest(
            model="err-429", messages=[ChatMessage(role="user", content="Hi")]
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ProviderError)
        assert result.code == "rate_limited"
        assert result.status_code == 429
        assert result.retryable

    async def test_numeric_provider_code_does_not_shadow_status_map(
        self, client: ProviderClient
    ) -> None:
        """A provider that echoes {"code": 429} still classifies as rate_limited.

        OpenRouter returns the HTTP status again as the body's ``code``. Taken
        verbatim it yields the untyped string "429", which no copy table has an
        entry for — so the UI falls through to its unknown-code text and the
        user is told to report a bug instead of to wait and resend.
        """
        request = ChatCompletionRequest(
            model="errc-429", messages=[ChatMessage(role="user", content="Hi")]
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ProviderError)
        assert result.code == "rate_limited"
        assert result.status_code == 429
        assert result.retryable

    async def test_insufficient_credits(self, client: ProviderClient) -> None:
        """402 is a typed, non-retryable cause, not a bare http_402."""
        request = ChatCompletionRequest(
            model="err-402", messages=[ChatMessage(role="user", content="Hi")]
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ProviderError)
        assert result.code == "insufficient_credits"
        assert result.status_code == 402
        # Retrying a payment failure just burns the turn again.
        assert not result.retryable

    async def test_insufficient_credits_with_echoed_code(self, client: ProviderClient) -> None:
        """The same 402, in OpenRouter's shape, classifies identically."""
        request = ChatCompletionRequest(
            model="errc-402", messages=[ChatMessage(role="user", content="Hi")]
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ProviderError)
        assert result.code == "insufficient_credits"
        assert not result.retryable

    async def test_typed_provider_code_still_wins(self, client: ProviderClient) -> None:
        """A non-numeric provider code is a real cause and is preserved."""
        request = ChatCompletionRequest(
            model="err-400", messages=[ChatMessage(role="user", content="Hi")]
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ProviderError)
        assert result.code == "bad_request"

    async def test_gateway_placeholder_message_is_replaced_by_the_real_reason(
        self, client: ProviderClient
    ) -> None:
        """metadata.raw wins over a placeholder ``message``.

        Surfacing "Provider returned error" verbatim tells the user nothing —
        it is the gateway's filler, not the cause. The upstream reason and the
        remedy sit in metadata and are the only actionable part.
        """
        request = ChatCompletionRequest(
            model="errm-429", messages=[ChatMessage(role="user", content="Hi")]
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ProviderError)
        assert result.code == "rate_limited"
        assert "Provider returned error" not in result.message
        assert "temporarily rate-limited upstream" in result.message
        # The way out travels with the reason.
        assert "route to another provider" in result.message

    async def test_error_envelope_under_http_200_is_an_error(self, client: ProviderClient) -> None:
        """A 200 whose body is {"error": {...}} classifies by the embedded code.

        Trusting the status line reports ``parse_error`` — unretryable — for
        what is really a retryable 429, so the turn dies instead of waiting.
        """
        request = ChatCompletionRequest(
            model="ok-err-429", messages=[ChatMessage(role="user", content="Hi")]
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ProviderError)
        assert result.code == "rate_limited"
        assert result.retryable

    async def test_error_envelope_under_http_200_while_streaming(
        self, client: ProviderClient
    ) -> None:
        """The streaming route must not read it as an empty completion.

        No SSE events arrive, so without the check the loop sees a turn that
        produced nothing and reports "the model finished without a reply" —
        blaming the model for the gateway's refusal, and not retrying.
        """
        request = ChatCompletionRequest(
            model="ok-err-429",
            messages=[ChatMessage(role="user", content="Hi")],
            stream=True,
        )
        items = [item async for item in client.chat_completion_stream(request)]
        assert items, "stream yielded nothing at all — the failure vanished"
        errors = [i for i in items if isinstance(i, ProviderError)]
        assert errors, f"expected a ProviderError, got {items!r}"
        assert errors[0].code == "rate_limited"
        assert errors[0].retryable

    async def test_event_stream_with_no_events_is_a_retryable_error(
        self, client: ProviderClient
    ) -> None:
        """An empty 200 stream must speak, not vanish.

        Yielding nothing ends the generator, which the retry wrapper cannot
        distinguish from a stream that finished normally — so the turn is
        reported as an empty completion, blaming the model for output the
        provider never sent, and nothing retries.
        """
        request = ChatCompletionRequest(
            model="empty-stream",
            messages=[ChatMessage(role="user", content="Hi")],
            stream=True,
        )
        items = [item async for item in client.chat_completion_stream(request)]
        assert items, "empty stream produced no item at all — the failure vanished"
        errors = [i for i in items if isinstance(i, ProviderError)]
        assert errors, f"expected a ProviderError, got {items!r}"
        assert errors[0].code == "empty_stream"
        assert errors[0].retryable

    async def test_a_normal_stream_is_not_reported_as_empty(self, client: ProviderClient) -> None:
        """Regression guard: a stream with content must not trip the check."""
        request = ChatCompletionRequest(
            model="mock-model",
            messages=[ChatMessage(role="user", content="Hi")],
            stream=True,
        )
        items = [item async for item in client.chat_completion_stream(request)]
        assert not [i for i in items if isinstance(i, ProviderError)]
        assert any(isinstance(i, StreamChunk) and i.delta.content for i in items)

    async def test_normal_success_is_not_mistaken_for_an_envelope(
        self, client: ProviderClient
    ) -> None:
        """Regression guard: a real response still parses as a response."""
        request = ChatCompletionRequest(
            model="mock-model", messages=[ChatMessage(role="user", content="Hi")]
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ChatCompletionResponse)
        assert result.message.content == "Hello, world!"

    async def test_server_error(self, client: ProviderClient) -> None:
        """Test that 500 is retryable."""
        request = ChatCompletionRequest(
            model="err-500", messages=[ChatMessage(role="user", content="Hi")]
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ProviderError)
        assert result.code == "server_error"
        assert result.retryable

    async def test_context_length_error(self, client: ProviderClient) -> None:
        """Test that 413 context length exceeded is not retryable."""
        request = ChatCompletionRequest(
            model="err-413", messages=[ChatMessage(role="user", content="Hi")]
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ProviderError)
        assert result.code == "context_length_exceeded"
        assert not result.retryable
        # Message is actionable
        assert "context window" in result.message
        assert "reduce" in result.message.lower()

    async def test_auth_failure_message(self, client: ProviderClient) -> None:
        """Test that 401 produces an actionable message."""
        request = ChatCompletionRequest(
            model="err-401", messages=[ChatMessage(role="user", content="Hi")]
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ProviderError)
        assert result.code == "auth_failed"
        assert not result.retryable
        # Message is actionable — points at the wizard, not a phantom CLI (TD-1102)
        assert "API key" in result.message
        assert "title bar" in result.message

    async def test_not_found(self, client: ProviderClient) -> None:
        """Test that 404 is handled."""
        request = ChatCompletionRequest(
            model="err-404", messages=[ChatMessage(role="user", content="Hi")]
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ProviderError)
        assert result.code == "not_found"

    async def test_error_in_stream(self, client: ProviderClient) -> None:
        """Test that error responses in streaming mode are handled."""
        request = ChatCompletionRequest(
            model="err-429", messages=[ChatMessage(role="user", content="Hi")]
        )
        # The fixture client has max_retries=0 — single-shot.
        errors: list[ProviderError] = []
        async for chunk in client.chat_completion_stream(request):
            if isinstance(chunk, ProviderError):
                errors.append(chunk)

        assert len(errors) == 1
        assert errors[0].code == "rate_limited"
        assert errors[0].retryable

    async def test_connection_refused(self) -> None:
        """Test connection error handling."""
        # A bound-then-closed loopback socket yields a port nothing listens
        # on.  127.0.0.1 rather than "localhost" skips name resolution: on
        # Windows the dual-stack getaddrinfo("localhost") can outlast a
        # tight connect deadline, and the refusal then surfaces as a
        # ConnectTimeout ("timeout") instead of a ConnectError (TD-1406).
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        client = ProviderClient(
            base_url=f"http://127.0.0.1:{port}",
            api_key="sk-test-key",
            timeout=TimeoutConfig(connect=2.0, read=2.0, total=5.0),
            retry_config=RetryConfig(max_retries=0),
        )
        request = ChatCompletionRequest(
            model="test", messages=[ChatMessage(role="user", content="Hi")]
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ProviderError)
        if sys.platform == "win32":
            assert result.code in {"connection_error", "timeout"}
        else:
            assert result.code == "connection_error"
        assert result.retryable


# ── Tests: Request building ────────────────────────────────────────────


class TestRequestBuilding:
    def test_tool_definition(self) -> None:
        """Test ToolDefinition serialization."""
        td = ToolDefinition(
            function=FunctionDefinition(
                name="get_weather",
                description="Get the weather",
                parameters={"type": "object", "properties": {"loc": {"type": "string"}}},
            )
        )
        assert td.function is not None
        d: dict[str, Any] = {
            "type": td.type,
            "function": {
                "name": td.function.name,
                "description": td.function.description,
                "parameters": td.function.parameters,
            },
        }
        assert d["function"]["name"] == "get_weather"
        props = d["function"]["parameters"]["properties"]
        assert props["loc"]["type"] == "string"

    def test_usage_from_api(self) -> None:
        """Test Usage parsing from API response."""
        data = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 30},
        }
        usage = Usage.from_api_dict(data)
        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50
        assert usage.cached_prompt_tokens == 30
        assert usage.total_tokens == 150

    def test_usage_empty(self) -> None:
        """Test Usage parsing with no data.

        Token counts default to zero because no tokens were reported spent.
        The cache figure defaults to ``None`` instead: zero cached tokens is
        a claim about the provider's cache, and a response that said nothing
        has not made it (TD-1811).
        """
        usage = Usage.from_api_dict(None)
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.cached_prompt_tokens is None

    def test_delta_tool_call(self) -> None:
        """Test DeltaToolCall creation."""
        tc = DeltaToolCall(
            index=0, id="call_1", function_name="get_weather", function_arguments='{"loc": "SF"}'
        )
        assert tc.index == 0
        assert tc.id == "call_1"
        assert tc.function_name == "get_weather"

    def test_timeout_config_defaults(self) -> None:
        """Test TimeoutConfig default values."""
        tc = TimeoutConfig()
        assert tc.connect == 30.0
        assert tc.read == 120.0
        assert tc.write == 30.0
        assert tc.total == 180.0

        timeout = tc.to_httpx_timeout()
        assert timeout.connect == 30.0
        assert timeout.read == 120.0
        assert timeout.pool == 180.0

    async def test_base_url_configurable(self) -> None:
        """Test that the base URL is configurable."""
        c = ProviderClient(
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-test",
        )
        assert c.base_url == "https://openrouter.ai/api/v1"
        await c.close()

        c2 = ProviderClient(
            base_url="http://localhost:8080/v1",
            api_key="sk-test",
        )
        assert c2.base_url == "http://localhost:8080/v1"
        await c2.close()

    async def test_timeout_config(self) -> None:
        """Test that timeout config is passed to httpx."""
        tc = TimeoutConfig(connect=5.0, read=30.0, write=10.0, total=60.0)
        c = ProviderClient(
            base_url="http://localhost:8080",
            api_key="sk-test",
            timeout=tc,
        )
        assert c.timeout.connect == 5.0
        assert c.timeout.read == 30.0
        assert c.timeout.total == 60.0
        await c.close()

    async def test_custom_client(self) -> None:
        """Test that a custom httpx client can be provided."""
        transport = httpx.ASGITransport(app=_mock_chat_app)  # type: ignore[arg-type]
        custom_client = httpx.AsyncClient(transport=transport)
        c = ProviderClient(
            base_url="http://mock/v1",
            api_key="sk-test",
            client=custom_client,
        )
        request = ChatCompletionRequest(
            model="mock-model",
            messages=[ChatMessage(role="user", content="Hi")],
        )
        result = await c.chat_completion(request)
        assert isinstance(result, ChatCompletionResponse)
        assert result.message.content == "Hello, world!"
        await c.close()

    async def test_base_url_with_v1_prefix(self) -> None:
        """Works with OpenRouter-style /v1/chat/completions path."""
        transport = httpx.ASGITransport(app=_mock_chat_app)  # type: ignore[arg-type]
        c = ProviderClient(
            base_url="http://mock/v1",
            api_key="sk-test",
            client=httpx.AsyncClient(transport=transport),
        )
        request = ChatCompletionRequest(
            model="mock-model",
            messages=[ChatMessage(role="user", content="Hi")],
        )
        result = await c.chat_completion(request)
        assert isinstance(result, ChatCompletionResponse)
        assert result.message.content == "Hello, world!"
        await c.close()

    async def test_base_url_without_v1_prefix(self) -> None:
        """Works with a bare endpoint (vLLM-style) — no /v1 prefix."""
        transport = httpx.ASGITransport(app=_mock_chat_app)  # type: ignore[arg-type]
        c = ProviderClient(
            base_url="http://mock",
            api_key="sk-test",
            client=httpx.AsyncClient(transport=transport),
        )
        request = ChatCompletionRequest(
            model="mock-model",
            messages=[ChatMessage(role="user", content="Hi")],
        )
        result = await c.chat_completion(request)
        assert isinstance(result, ChatCompletionResponse)
        assert result.message.content == "Hello, world!"
        await c.close()

    async def test_base_url_with_trailing_slash(self) -> None:
        """Works with a trailing slash on the base URL (no double slash)."""
        transport = httpx.ASGITransport(app=_mock_chat_app)  # type: ignore[arg-type]
        c = ProviderClient(
            base_url="http://mock/v1/",
            api_key="sk-test",
            client=httpx.AsyncClient(transport=transport),
        )
        request = ChatCompletionRequest(
            model="mock-model",
            messages=[ChatMessage(role="user", content="Hi")],
        )
        result = await c.chat_completion(request)
        assert isinstance(result, ChatCompletionResponse)
        assert result.message.content == "Hello, world!"
        await c.close()


# ── Tests: Reasoning deltas (TD-1901) ──────────────────────────────────


class TestReasoningDeltas:
    """A reasoning model's thinking arrives in a field of its own.

    Captured 2026-08-17 from Ollama serving ``qwen3.8:27b``: every
    reasoning chunk carries ``"content": ""`` alongside ``"reasoning"``.
    An empty string is falsy, so a parser that reads only ``content``
    yields nothing at all for the whole thinking phase.
    """

    @staticmethod
    def _chunk(client: ProviderClient, delta: dict[str, Any]) -> StreamChunk:
        parsed = client._parse_stream_chunk(
            json.dumps({"id": "c1", "choices": [{"index": 0, "delta": delta}]})
        )
        assert parsed is not None
        return parsed

    def test_ollama_reasoning_field(self, client: ProviderClient) -> None:
        """``reasoning`` is parsed and does not become content."""
        chunk = self._chunk(client, {"content": "", "reasoning": "The user"})
        assert chunk.delta.reasoning == "The user"
        # "" must not survive as content: a falsy-but-present value is what
        # the loop's emission gate trips over.
        assert not chunk.delta.content

    def test_deepseek_reasoning_content_field(self, client: ProviderClient) -> None:
        """The other spelling in the wild parses to the same place."""
        chunk = self._chunk(client, {"reasoning_content": "Let me think"})
        assert chunk.delta.reasoning == "Let me think"

    def test_content_chunk_carries_no_reasoning(self, client: ProviderClient) -> None:
        """An ordinary content delta is unchanged by this story."""
        chunk = self._chunk(client, {"content": "Hello"})
        assert chunk.delta.content == "Hello"
        assert chunk.delta.reasoning is None

    def test_interleaved_reasoning_and_content(self, client: ProviderClient) -> None:
        """A stream that thinks, answers, then thinks again keeps them apart."""
        script: list[dict[str, Any]] = [
            {"content": "", "reasoning": "Think"},
            {"content": "", "reasoning": " harder"},
            {"content": "Answer", "reasoning": None},
            {"content": " here"},
        ]
        chunks = [self._chunk(client, d) for d in script]
        reasoning = "".join(c.delta.reasoning or "" for c in chunks)
        content = "".join(c.delta.content or "" for c in chunks)
        assert reasoning == "Think harder"
        assert content == "Answer here"

    def test_neither_field_present(self, client: ProviderClient) -> None:
        """A role-only opening chunk parses without inventing either."""
        chunk = self._chunk(client, {"role": "assistant"})
        assert chunk.delta.content is None
        assert chunk.delta.reasoning is None

    def test_empty_reasoning_string_is_not_a_reasoning_chunk(self, client: ProviderClient) -> None:
        """``reasoning: ""`` normalises to None rather than an empty event."""
        chunk = self._chunk(client, {"content": "x", "reasoning": ""})
        assert chunk.delta.reasoning is None
