"""Async provider client for OpenAI-compatible chat completion APIs.

One async ``httpx`` client, configurable to any OpenAI-compatible endpoint
(OpenRouter, local vLLM, etc.) with no code change. Supports streaming,
tool/function calling, and configurable timeouts.

The client reads the API key from the OS keychain (never from config or
environment files) — see :mod:`tstd.keychain`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from httpx_sse import aconnect_sse

from .logging import get_logger

log = get_logger("tstd.provider")

# Maps HTTP status codes to friendly error names.
_STATUS_CODE_MAP: dict[int, str] = {
    400: "bad_request",
    401: "auth_failed",
    403: "forbidden",
    404: "not_found",
    413: "context_length_exceeded",
    429: "rate_limited",
    500: "server_error",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "gateway_timeout",
}

# ── Data models ────────────────────────────────────────────────────────


@dataclass
class FunctionDefinition:
    """Definition of a function the model may call."""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolDefinition:
    """A tool definition sent in the request."""

    type: Literal["function"] = "function"
    function: FunctionDefinition | None = None


@dataclass
class FunctionCall:
    """A function call returned by the model."""

    name: str
    arguments: str  # JSON string, to be parsed by the tool dispatcher


@dataclass
class ToolCall:
    """A tool call returned by the model's choice."""

    id: str
    type: Literal["function"] = "function"
    function: FunctionCall | None = None


@dataclass
class ChatMessage:
    """A single message in the chat conversation."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


@dataclass
class ChatCompletionRequest:
    """A chat completion request to the OpenAI-compatible API."""

    model: str
    messages: list[ChatMessage]
    tools: list[ToolDefinition] | None = None
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: list[str] | None = None
    extra_body: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the OpenAI-compatible JSON request body."""
        d: dict[str, Any] = {
            "model": self.model,
            "messages": [self._message_to_dict(m) for m in self.messages],
        }
        if self.tools:
            d["tools"] = [
                {
                    "type": t.type,
                    "function": {
                        "name": t.function.name,
                        "description": t.function.description,
                        "parameters": t.function.parameters,
                    }
                    if t.function
                    else {},
                }
                for t in self.tools
            ]
        if self.stream:
            d["stream"] = True
            d["stream_options"] = {"include_usage": True}
        if self.max_tokens is not None:
            d["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            d["temperature"] = self.temperature
        if self.top_p is not None:
            d["top_p"] = self.top_p
        if self.stop is not None:
            d["stop"] = self.stop
        if self.extra_body:
            d.update(self.extra_body)
        return d

    @staticmethod
    def _message_to_dict(msg: ChatMessage) -> dict[str, Any]:
        d: dict[str, Any] = {"role": msg.role}
        if msg.content is not None:
            d["content"] = msg.content
        if msg.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    }
                    if tc.function
                    else {},
                }
                for tc in msg.tool_calls
            ]
        if msg.tool_call_id is not None:
            d["tool_call_id"] = msg.tool_call_id
        return d


@dataclass
class Usage:
    """Token usage returned by the API."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_api_dict(cls, data: dict[str, Any] | None) -> Usage:
        """Parse usage from an API response dict."""
        if not data:
            return cls()
        return cls(
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
            cached_prompt_tokens=data.get("prompt_tokens_details", {}).get("cached_tokens", 0)
            if isinstance(data.get("prompt_tokens_details"), dict)
            else 0,
            total_tokens=data.get("total_tokens", 0),
        )


@dataclass
class Delta:
    """A streaming delta — a chunk of assistant output."""

    content: str | None = None
    tool_calls: list[DeltaToolCall] | None = None


@dataclass
class DeltaToolCall:
    """A partial tool call received during streaming."""

    index: int
    id: str | None = None
    function_name: str | None = None
    function_arguments: str | None = None


@dataclass
class StreamChunk:
    """A single chunk from a streaming response."""

    id: str
    delta: Delta
    finish_reason: str | None = None
    usage: Usage | None = None


@dataclass
class ChatCompletionResponse:
    """A complete (non-streaming) chat completion response."""

    id: str
    model: str
    message: ChatMessage
    finish_reason: str | None = None
    usage: Usage | None = None

    @classmethod
    def from_api_dict(cls, data: dict[str, Any]) -> ChatCompletionResponse:
        """Parse a non-streaming response from the API."""
        choice = data["choices"][0]
        msg_data = choice["message"]

        tool_calls = None
        if msg_data.get("tool_calls"):
            tool_calls = [
                ToolCall(
                    id=tc["id"],
                    type=tc.get("type", "function"),
                    function=FunctionCall(
                        name=tc["function"]["name"],
                        arguments=tc["function"]["arguments"],
                    ),
                )
                for tc in msg_data["tool_calls"]
            ]

        return cls(
            id=data["id"],
            model=data.get("model", ""),
            message=ChatMessage(
                role=msg_data.get("role", "assistant"),
                content=msg_data.get("content"),
                tool_calls=tool_calls,
            ),
            finish_reason=choice.get("finish_reason"),
            usage=Usage.from_api_dict(data.get("usage")),
        )


@dataclass
class ProviderError:
    """A typed error from the provider API."""

    code: str
    message: str
    status_code: int = 0
    retryable: bool = False


# ── Timeout configuration ──────────────────────────────────────────────


@dataclass
class TimeoutConfig:
    """Timeout configuration for API requests.

    All values are in seconds.
    """

    connect: float = 30.0
    """Timeout for establishing the connection."""

    read: float = 120.0
    """Timeout for reading the response body."""

    write: float = 30.0
    """Timeout for writing the request body."""

    total: float = 180.0
    """Total timeout for the entire request."""

    def to_httpx_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect,
            read=self.read,
            write=self.write,
            pool=self.total,
        )


# ── Provider client ────────────────────────────────────────────────────


class ProviderClient:
    """Async HTTP client for an OpenAI-compatible chat completion API.

    Usage::

        api_key = "sk-your-key-here"  # loaded from keychain via from_keychain()
        client = ProviderClient(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
        )
        request = ChatCompletionRequest(
            model="moonshotai/kimi-k3",
            messages=[ChatMessage(role="user", content="Hello")],
        )
        # Non-streaming
        response = await client.chat_completion(request)

        # Streaming
        async for chunk in client.chat_completion_stream(request):
            print(chunk.delta.content)
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: TimeoutConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout or TimeoutConfig()

        self._client = client or httpx.AsyncClient(
            timeout=self.timeout.to_httpx_timeout(),
            follow_redirects=True,
        )

        log.info(
            "provider client created",
            extra={
                "extra_fields": {
                    "base_url": self.base_url,
                    "timeout": str(self.timeout),
                }
            },
        )

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse | ProviderError:
        """Send a non-streaming chat completion request.

        Args:
            request: The chat completion request.

        Returns:
            The response, or a ``ProviderError`` on failure.
        """
        body = request.to_dict()
        # Ensure stream is False
        body.pop("stream", None)
        body.pop("stream_options", None)

        try:
            response = await self._client.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers=self._headers(),
            )
        except httpx.TimeoutException as e:
            log.warning("provider request timed out", extra={"extra_fields": {"error": str(e)}})
            return ProviderError(
                code="timeout",
                message=f"Request timed out: {e}",
                retryable=True,
            )
        except httpx.ConnectError as e:
            log.warning("provider connection failed", extra={"extra_fields": {"error": str(e)}})
            return ProviderError(
                code="connection_error",
                message=f"Failed to connect to {self.base_url}: {e}",
                retryable=True,
            )
        except httpx.HTTPError as e:
            log.warning("provider request failed", extra={"extra_fields": {"error": str(e)}})
            return ProviderError(
                code="request_error",
                message=str(e),
                retryable=True,
            )

        if response.is_success:
            try:
                data = response.json()
                return ChatCompletionResponse.from_api_dict(data)
            except (ValueError, KeyError, IndexError) as e:
                log.error(
                    "failed to parse provider response",
                    extra={"extra_fields": {"error": str(e), "body": response.text[:500]}},
                )
                return ProviderError(
                    code="parse_error",
                    message=f"Failed to parse response: {e}",
                    status_code=response.status_code,
                    retryable=False,
                )

        # Handle error responses
        return self._parse_error(response)

    async def chat_completion_stream(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[StreamChunk | ProviderError]:
        """Send a streaming chat completion request and yield chunks.

        Args:
            request: The chat completion request.

        Yields:
            ``StreamChunk`` for each delta, or ``ProviderError`` on failure.
        """
        body = request.to_dict()
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}

        try:
            async with aconnect_sse(
                self._client,
                "POST",
                f"{self.base_url}/chat/completions",
                json=body,
                headers=self._headers(),
            ) as sse:
                # Check for error responses
                if not sse.response.is_success:
                    error_body = await sse.response.aread()
                    yield self._parse_error(
                        sse.response, error_body.decode() if error_body else None
                    )
                    return

                async for event in sse.aiter_sse():
                    chunk = self._parse_stream_chunk(event.data)
                    if chunk is not None:
                        yield chunk

        except httpx.TimeoutException as e:
            log.warning("provider stream timed out", extra={"extra_fields": {"error": str(e)}})
            yield ProviderError(
                code="timeout",
                message=f"Stream timed out: {e}",
                retryable=True,
            )
        except httpx.ConnectError as e:
            log.warning(
                "provider stream connection failed",
                extra={"extra_fields": {"error": str(e)}},
            )
            yield ProviderError(
                code="connection_error",
                message=f"Failed to connect to {self.base_url}: {e}",
                retryable=True,
            )
        except httpx.HTTPError as e:
            log.warning("provider stream failed", extra={"extra_fields": {"error": str(e)}})
            yield ProviderError(
                code="request_error",
                message=str(e),
                retryable=True,
            )

    def _headers(self) -> dict[str, str]:
        """Build the request headers."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _parse_error(
        self,
        response: httpx.Response,
        body: str | None = None,
    ) -> ProviderError:
        """Parse an error response from the API."""
        status = response.status_code
        body_text = body or response.text

        # Try to extract the error message from the JSON body
        try:
            data = response.json() if body is None else __import__("json").loads(body)
            # OpenAI/OpenRouter format: {"error": {"message": "...", "code": "..."}}
            if "error" in data and isinstance(data["error"], dict):
                err = data["error"]
                msg = err.get("message", body_text[:200])
                code = err.get("code", f"http_{status}")
                # Map generic http_XXX codes to friendly names
                if code.startswith("http_"):
                    mapped = _STATUS_CODE_MAP.get(status)
                    if mapped:
                        code = mapped
                return ProviderError(
                    code=code,
                    message=msg,
                    status_code=status,
                    retryable=status in (429, 500, 502, 503, 504),
                )
        except (ValueError, KeyError, TypeError):
            pass

        code = _STATUS_CODE_MAP.get(status, f"http_{status}")
        retryable = status in (429, 500, 502, 503, 504)

        return ProviderError(
            code=code,
            message=body_text[:200],
            status_code=status,
            retryable=retryable,
        )

    def _parse_stream_chunk(self, raw: str) -> StreamChunk | None:
        """Parse a single SSE event from the streaming response.

        Args:
            raw: The raw SSE event data (without the ``data: `` prefix).

        Returns:
            A ``StreamChunk``, or ``None`` for the ``[DONE]`` signal or
            non-data events.
        """
        # Handle the terminal signal
        line = raw.strip()
        if not line:
            return None
        if line == "[DONE]":
            return None

        try:
            data = __import__("json").loads(line)
        except ValueError:
            return None

        # Parse usage from the final chunk, if present
        usage = None
        if "usage" in data and data["usage"] is not None:
            usage = Usage.from_api_dict(data["usage"])

        # Parse choices
        choices = data.get("choices", [])
        if not choices:
            # Some providers send usage-only chunks; capture the usage
            if usage:
                return StreamChunk(
                    id=data.get("id", ""),
                    delta=Delta(),
                    finish_reason=None,
                    usage=usage,
                )
            return None

        choice = choices[0]
        delta_data = choice.get("delta", {})
        finish_reason = choice.get("finish_reason")

        # Parse content delta
        content = delta_data.get("content")

        # Parse tool call deltas
        tool_calls = None
        if "tool_calls" in delta_data:
            tool_calls = []
            for tc in delta_data["tool_calls"]:
                tc_delta = DeltaToolCall(
                    index=tc.get("index", 0),
                    id=tc.get("id"),
                    function_name=tc.get("function", {}).get("name"),
                    function_arguments=tc.get("function", {}).get("arguments"),
                )
                tool_calls.append(tc_delta)

        return StreamChunk(
            id=data.get("id", ""),
            delta=Delta(content=content, tool_calls=tool_calls),
            finish_reason=finish_reason,
            usage=usage,
        )

    @classmethod
    async def from_keychain(
        cls,
        base_url: str,
        provider_name: str = "openrouter",
        timeout_config: TimeoutConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> ProviderClient:
        """Create a client with the API key loaded from the OS keychain.

        Args:
            base_url: The base URL of the OpenAI-compatible API.
            provider_name: The provider name used as the keychain account
                suffix (default ``openrouter``).
            timeout_config: Optional timeout configuration.
            client: Optional pre-configured ``httpx.AsyncClient``.

        Returns:
            A new ``ProviderClient`` with the key loaded from the keychain.

        Raises:
            KeychainError: If the API key is not found in the keychain.
        """
        from .keychain import get_api_key

        api_key = await get_api_key(provider_name)
        return cls(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout_config,
            client=client,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client and release resources."""
        await self._client.aclose()

    async def __aenter__(self) -> ProviderClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
