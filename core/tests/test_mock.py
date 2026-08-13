"""Tests for the mock provider.

Verifies every script kind, deterministic behaviour, offline operation,
and realistic usage numbers.
"""

from __future__ import annotations

import pytest

from tstd.mock import (
    DEFAULT_COMPLETION_TOKENS,
    DEFAULT_PROMPT_TOKENS,
    MockProvider,
    Script,
)
from tstd.provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ProviderError,
    StreamChunk,
)


@pytest.fixture
def mock() -> MockProvider:
    return MockProvider()


@pytest.fixture
def basic_request() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="Hi")],
    )


@pytest.fixture
def stream_request() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="Hi")],
        stream=True,
    )


# ── Non-streaming ──────────────────────────────────────────────────────


class TestNonStreaming:
    async def test_plain_text(
        self, mock: MockProvider, basic_request: ChatCompletionRequest
    ) -> None:
        mock.script("test-model", Script(kind="text", content="Hello, world!"))
        result = await mock.chat_completion(basic_request)
        assert isinstance(result, ChatCompletionResponse)
        assert result.message.content == "Hello, world!"
        assert result.finish_reason == "stop"
        assert result.usage is not None
        assert result.usage.prompt_tokens == DEFAULT_PROMPT_TOKENS
        assert result.usage.completion_tokens == DEFAULT_COMPLETION_TOKENS

    async def test_tool_call(
        self, mock: MockProvider, basic_request: ChatCompletionRequest
    ) -> None:
        mock.script(
            "test-model",
            Script(
                kind="tool_call",
                tool_name="get_weather",
                tool_arguments='{"location": "SF"}',
            ),
        )
        result = await mock.chat_completion(basic_request)
        assert isinstance(result, ChatCompletionResponse)
        assert result.message.tool_calls is not None
        assert len(result.message.tool_calls) == 1
        tc = result.message.tool_calls[0]
        assert tc.function is not None
        assert tc.function.name == "get_weather"
        assert tc.function.arguments == '{"location": "SF"}'
        assert result.finish_reason == "tool_calls"

    async def test_error(self, mock: MockProvider, basic_request: ChatCompletionRequest) -> None:
        mock.script(
            "test-model",
            Script(
                kind="error",
                error_code="server_error",
                status_code=500,
                content="Internal error",
            ),
        )
        result = await mock.chat_completion(basic_request)
        assert isinstance(result, ProviderError)
        assert result.code == "server_error"
        assert result.status_code == 500
        assert result.retryable

    async def test_rate_limit(
        self, mock: MockProvider, basic_request: ChatCompletionRequest
    ) -> None:
        mock.script("test-model", Script(kind="rate_limit", content="Slow down"))
        result = await mock.chat_completion(basic_request)
        assert isinstance(result, ProviderError)
        assert result.code == "rate_limited"
        assert result.status_code == 429
        assert result.retryable

    async def test_malformed(
        self, mock: MockProvider, basic_request: ChatCompletionRequest
    ) -> None:
        mock.script("test-model", Script(kind="malformed"))
        result = await mock.chat_completion(basic_request)
        assert isinstance(result, ProviderError)
        assert result.code == "parse_error"
        assert not result.retryable

    async def test_default_script(
        self, mock: MockProvider, basic_request: ChatCompletionRequest
    ) -> None:
        """Unscripted models use the default."""
        result = await mock.chat_completion(basic_request)
        assert isinstance(result, ChatCompletionResponse)
        assert result.message.content == "Hello from mock provider"

    async def test_custom_default(self, basic_request: ChatCompletionRequest) -> None:
        mock = MockProvider(default=Script(kind="text", content="Custom default"))
        result = await mock.chat_completion(basic_request)
        assert isinstance(result, ChatCompletionResponse)
        assert result.message.content == "Custom default"

    async def test_records_calls(
        self, mock: MockProvider, basic_request: ChatCompletionRequest
    ) -> None:
        mock.script("test-model", Script(kind="text", content="Reply"))
        await mock.chat_completion(basic_request)
        assert len(mock.calls) == 1
        assert mock.calls[0].model == "test-model"
        assert mock.calls[0].messages[0].content == "Hi"

    async def test_custom_usage(
        self, mock: MockProvider, basic_request: ChatCompletionRequest
    ) -> None:
        mock.script(
            "test-model",
            Script(
                kind="text",
                content="X",
                prompt_tokens=500,
                completion_tokens=100,
                cached_tokens=200,
            ),
        )
        result = await mock.chat_completion(basic_request)
        assert isinstance(result, ChatCompletionResponse)
        assert result.usage is not None
        assert result.usage.prompt_tokens == 500
        assert result.usage.completion_tokens == 100
        assert result.usage.cached_prompt_tokens == 200
        assert result.usage.total_tokens == 600


# ── Streaming ──────────────────────────────────────────────────────────


class TestStreaming:
    async def test_plain_text_stream(
        self, mock: MockProvider, stream_request: ChatCompletionRequest
    ) -> None:
        mock.script("test-model", Script(kind="stream", content="Hello from stream"))
        chunks: list[StreamChunk] = []
        async for chunk in mock.chat_completion_stream(stream_request):
            if isinstance(chunk, StreamChunk):
                chunks.append(chunk)

        texts = [c.delta.content or "" for c in chunks if c.delta.content]
        assert "".join(texts) == "Hello from stream"

        # Last chunk should have finish_reason and usage
        final = chunks[-1]
        assert final.finish_reason == "stop"
        assert final.usage is not None
        assert final.usage.prompt_tokens == DEFAULT_PROMPT_TOKENS

    async def test_tool_call_stream(
        self, mock: MockProvider, stream_request: ChatCompletionRequest
    ) -> None:
        mock.script(
            "test-model",
            Script(
                kind="tool_call",
                tool_name="get_weather",
                tool_arguments='{"loc": "SF"}',
            ),
        )
        chunks: list[StreamChunk] = []
        async for chunk in mock.chat_completion_stream(stream_request):
            if isinstance(chunk, StreamChunk):
                chunks.append(chunk)

        tool_chunks = [c for c in chunks if c.delta.tool_calls]
        assert len(tool_chunks) >= 1
        tcs = tool_chunks[0].delta.tool_calls
        assert tcs is not None
        assert tcs[0].id == "call_mock_1"

        # Last chunk should have tool_calls finish reason
        assert chunks[-1].finish_reason == "tool_calls"

    async def test_error_in_stream(
        self, mock: MockProvider, stream_request: ChatCompletionRequest
    ) -> None:
        mock.script("test-model", Script(kind="error", content="Stream error"))
        errors: list[ProviderError] = []
        async for chunk in mock.chat_completion_stream(stream_request):
            if isinstance(chunk, ProviderError):
                errors.append(chunk)

        assert len(errors) == 1
        assert errors[0].code == "server_error"

    async def test_rate_limit_in_stream(
        self, mock: MockProvider, stream_request: ChatCompletionRequest
    ) -> None:
        mock.script("test-model", Script(kind="rate_limit"))
        errors: list[ProviderError] = []
        async for chunk in mock.chat_completion_stream(stream_request):
            if isinstance(chunk, ProviderError):
                errors.append(chunk)

        assert len(errors) == 1
        assert errors[0].code == "rate_limited"

    async def test_deterministic(
        self, mock: MockProvider, basic_request: ChatCompletionRequest
    ) -> None:
        """Same script produces the same result every time."""
        mock.script("test-model", Script(kind="text", content="Deterministic"))
        r1 = await mock.chat_completion(basic_request)
        r2 = await mock.chat_completion(basic_request)
        assert isinstance(r1, ChatCompletionResponse)
        assert isinstance(r2, ChatCompletionResponse)
        assert r1.message.content == r2.message.content

    async def test_no_network(
        self, mock: MockProvider, basic_request: ChatCompletionRequest
    ) -> None:
        """Mock never touches the network — no httpx client needed."""
        mock.script("test-model", Script(kind="text", content="Offline"))
        result = await mock.chat_completion(basic_request)
        assert isinstance(result, ChatCompletionResponse)
        assert result.message.content == "Offline"
