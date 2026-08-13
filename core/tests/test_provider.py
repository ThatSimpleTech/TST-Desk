"""Tests for the provider client — request building, response parsing,
streaming, tool calling, error handling, and timeouts.

Uses an in-process ASGI mock server so no network is needed.
"""

from __future__ import annotations

import json
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

    # Error simulation: model name "err-{status_code}"
    if model.startswith("err-"):
        status = int(model.split("-")[1])
        data = {"error": {"message": f"Simulated error: {status}"}}
        await _send_response(send, status, data)
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
        # Message is actionable
        assert "API key" in result.message
        assert "keychain" in result.message

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
        client = ProviderClient(
            base_url="http://localhost:1",
            api_key="sk-test-key",
            timeout=TimeoutConfig(connect=0.1, read=0.1, total=0.5),
            retry_config=RetryConfig(max_retries=0),
        )
        request = ChatCompletionRequest(
            model="test", messages=[ChatMessage(role="user", content="Hi")]
        )
        result = await client.chat_completion(request)
        assert isinstance(result, ProviderError)
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
        """Test Usage parsing with no data."""
        usage = Usage.from_api_dict(None)
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.cached_prompt_tokens == 0

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
