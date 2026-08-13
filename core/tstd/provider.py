"""Async provider client for OpenAI-compatible chat completion APIs.

One async ``httpx`` client, configurable to any OpenAI-compatible endpoint
(OpenRouter, local vLLM, etc.) with no code change. Supports streaming,
tool/function calling, and configurable timeouts.

The client reads the API key from the OS keychain (never from config or
environment files) — see :mod:`tstd.keychain`.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
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
    retry_after: float | None = None
    """Seconds to wait before retrying, from a ``Retry-After`` header."""


# ── Retry configuration ────────────────────────────────────────────────


@dataclass
class RetryConfig:
    """Retry policy for provider calls.

    Applies to retryable failures (HTTP 429, 5xx, and connection errors).
    Delays grow exponentially with jitter, capped at *max_delay*.
    ``Retry-After`` headers override the computed delay when present.
    """

    max_retries: int = 3
    """Maximum number of retries after the initial attempt (0 = no retry)."""

    initial_delay: float = 1.0
    """Base delay for the first retry, in seconds."""

    max_delay: float = 60.0
    """Cap for the exponential delay, in seconds."""

    jitter_factor: float = 0.25
    """Fraction of the base delay added as random jitter (0 = none, 1 = 100%)."""

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {self.max_retries}")
        if self.initial_delay <= 0:
            raise ValueError(f"initial_delay must be > 0, got {self.initial_delay}")
        if self.max_delay <= 0:
            raise ValueError(f"max_delay must be > 0, got {self.max_delay}")
        if self.jitter_factor < 0 or self.jitter_factor > 1:
            raise ValueError(f"jitter_factor must be in [0, 1], got {self.jitter_factor}")


def retry_delay(attempt: int, config: RetryConfig, retry_after: float | None = None) -> float:
    """Compute the sleep delay before retry *attempt* (1-based).

    Uses exponential backoff (*initial_delay* * 2**n) capped at *max_delay*,
    plus uniform jitter of up to *jitter_factor* of the base.  If
    *retry_after* is given (from a ``Retry-After`` header), the delay is at
    least *retry_after*.

    Parameters
    ----------
    attempt:
        1-based retry attempt number.
    config:
        Retry policy.
    retry_after:
        Minimum seconds to wait, from a ``Retry-After`` header.

    Returns
    -------
    The delay in seconds.
    """
    base: float = min(config.initial_delay * (2 ** (attempt - 1)), config.max_delay)
    if retry_after is not None:
        base = max(base, retry_after)
    return base * (1 + config.jitter_factor * random.random())


async def retry_call(
    call: Callable[[], Awaitable[ChatCompletionResponse | ProviderError]],
    config: RetryConfig,
) -> ChatCompletionResponse | ProviderError:
    """Call *call* and retry retryable errors with exponential backoff + jitter.

    Stops after *config.max_retries* retries and returns the last error.
    Non-retryable errors (auth, context-length, parse) are returned immediately
    without retrying.

    Parameters
    ----------
    call:
        Async callable that returns a response or a typed error.
    config:
        Retry policy.

    Returns
    -------
    The first successful response, or the last ``ProviderError`` after all
    retries are exhausted.
    """
    attempts = 0
    while True:
        result = await call()
        if isinstance(result, ChatCompletionResponse):
            return result

        # ProviderError
        if not result.retryable or attempts >= config.max_retries:
            return result

        attempts += 1
        delay = retry_delay(attempts, config, result.retry_after)
        log.warning(
            "provider call failed, retrying",
            extra={
                "extra_fields": {
                    "attempt": attempts,
                    "max_retries": config.max_retries,
                    "code": result.code,
                    "status_code": result.status_code,
                    "delay": f"{delay:.2f}s",
                }
            },
        )
        await asyncio.sleep(delay)


async def retry_stream(
    open_stream: Callable[[], AsyncIterator[StreamChunk | ProviderError]],
    config: RetryConfig,
) -> AsyncIterator[StreamChunk | ProviderError]:
    """Open a stream, retrying retryable errors on the **first** item only.

    Once the first content chunk has been yielded, the stream is committed
    and no further retries are attempted.  Mid-stream failures are passed
    through as-is (never retried).

    Parameters
    ----------
    open_stream:
        Factory that returns an async iterator of stream chunks or errors.
    config:
        Retry policy.

    Yields
    ------
    ``StreamChunk`` for each delta (or ``ProviderError`` on failure).
    """
    attempts = 0
    while True:
        stream = open_stream()
        try:
            first = await anext(stream)
        except StopAsyncIteration:
            return

        if isinstance(first, ProviderError) and first.retryable and attempts < config.max_retries:
            attempts += 1
            delay = retry_delay(attempts, config, first.retry_after)
            log.warning(
                "provider stream connection failed, retrying",
                extra={
                    "extra_fields": {
                        "attempt": attempts,
                        "max_retries": config.max_retries,
                        "code": first.code,
                        "status_code": first.status_code,
                        "delay": f"{delay:.2f}s",
                    }
                },
            )
            await asyncio.sleep(delay)
            continue

        yield first
        async for item in stream:
            yield item
        return


# ── Actionable error messages ─────────────────────────────────────────


def auth_failure_message() -> str:
    """Actionable guidance for authentication failures (HTTP 401)."""
    return (
        "Authentication failed. Fix one of these and retry: "
        "1) store a valid API key for this provider in the OS keychain "
        "(tstd keychain set <provider>), "
        "2) check that the provider base_url in config.yaml "
        "matches the key's provider, "
        "3) verify the key has not expired or been revoked."
    )


def context_length_message(provider_detail: str = "") -> str:
    """Actionable guidance for context-window-exceeded errors (HTTP 413)."""
    msg = (
        "The request exceeded the model's context window. "
        "Reduce the conversation or tool results (start a new session, "
        "trim the prompt), or switch the tier to a model with a larger "
        "context window in config.yaml."
    )
    if provider_detail:
        msg = f"{msg} Provider detail: {provider_detail}"
    return msg


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value to seconds.

    Handles both decimal seconds and HTTP-date formats.
    Returns ``None`` when the header is missing or unparseable.
    """
    if value is None:
        return None
    # Try seconds (integer or decimal)
    try:
        return float(value)
    except ValueError:
        pass
    # Try HTTP-date format (RFC 7231)
    try:
        dt = parsedate_to_datetime(value)
        now = datetime.now(UTC)
        return max(0.0, (dt - now).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


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
            model="<your-model-slug>",  # comes from config.yaml, never hardcoded
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
        retry_config: RetryConfig | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout or TimeoutConfig()
        self.retry_config = retry_config or RetryConfig()

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
        """Send a non-streaming chat completion request with retries.

        Retries on retryable errors (HTTP 429, 5xx, connection errors) with
        exponential backoff and jitter, bounded by *retry_config*.
        ``Retry-After`` headers are honoured when present.

        Args:
            request: The chat completion request.

        Returns:
            The response, or a ``ProviderError`` on failure (after retry
            exhaustion for retryable errors).
        """
        body = request.to_dict()
        # Ensure stream is False
        body.pop("stream", None)
        body.pop("stream_options", None)

        async def _attempt() -> ChatCompletionResponse | ProviderError:
            try:
                response = await self._client.post(
                    f"{self.base_url}/chat/completions",
                    json=body,
                    headers=self._headers(),
                )
            except httpx.TimeoutException as e:
                log.warning(
                    "provider request timed out",
                    extra={"extra_fields": {"error": str(e)}},
                )
                return ProviderError(
                    code="timeout",
                    message=f"Request timed out: {e}",
                    retryable=True,
                )
            except httpx.ConnectError as e:
                log.warning(
                    "provider connection failed",
                    extra={"extra_fields": {"error": str(e)}},
                )
                return ProviderError(
                    code="connection_error",
                    message=f"Failed to connect to {self.base_url}: {e}",
                    retryable=True,
                )
            except httpx.HTTPError as e:
                log.warning(
                    "provider request failed",
                    extra={"extra_fields": {"error": str(e)}},
                )
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
                        extra={
                            "extra_fields": {
                                "error": str(e),
                                "body": response.text[:500],
                            }
                        },
                    )
                    return ProviderError(
                        code="parse_error",
                        message=f"Failed to parse response: {e}",
                        status_code=response.status_code,
                        retryable=False,
                    )

            # Handle error responses
            return self._parse_error(response)

        return await retry_call(_attempt, self.retry_config)

    async def chat_completion_stream(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[StreamChunk | ProviderError]:
        """Send a streaming chat completion request and yield chunks.

        Retries on retryable errors **only for the initial connection**
        (before any content chunk has been yielded).  Mid-stream failures
        are passed through cleanly without retrying.

        If the stream ends while a tool call is in flight, a
        ``stream_interrupted`` error is yielded so the consumer never sees
        a half-parsed tool call.

        Args:
            request: The chat completion request.

        Yields:
            ``StreamChunk`` for each delta, or ``ProviderError`` on failure.
        """
        body = request.to_dict()
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}

        async def _stream_once() -> AsyncIterator[StreamChunk | ProviderError]:
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
                            sse.response,
                            error_body.decode() if error_body else None,
                        )
                        return

                    in_tool_call = False
                    saw_finish = False
                    try:
                        async for event in sse.aiter_sse():
                            try:
                                chunk = self._parse_stream_chunk(event.data)
                            except ValueError as e:
                                # Malformed chunk mid-tool-call => fail cleanly
                                if in_tool_call:
                                    log.warning(
                                        "malformed chunk mid-tool-call, discarding",
                                        extra={
                                            "extra_fields": {
                                                "error": str(e)[:100],
                                            }
                                        },
                                    )
                                    yield ProviderError(
                                        code="parse_error",
                                        message="Malformed stream data arrived while a "
                                        "tool call was in progress; the partial "
                                        "tool call was discarded.",
                                        retryable=False,
                                    )
                                    return
                                log.warning(
                                    "skipping malformed stream event",
                                    extra={"extra_fields": {"error": str(e)[:100]}},
                                )
                                continue
                            if chunk is None:
                                continue
                            if chunk.delta.tool_calls:
                                in_tool_call = True
                            if chunk.finish_reason:
                                saw_finish = True
                            yield chunk
                    except httpx.HTTPError as e:
                        # Mid-stream connection drop
                        word = "tool call" if in_tool_call and not saw_finish else "response"
                        log.warning(
                            "provider stream interrupted mid-response",
                            extra={"extra_fields": {"error": str(e)}},
                        )
                        yield ProviderError(
                            code="stream_interrupted",
                            message=f"Stream interrupted mid-{word}: {e}. "
                            "Any partial content was discarded.",
                            retryable=False,
                        )
                        return

                    if in_tool_call and not saw_finish:
                        yield ProviderError(
                            code="stream_interrupted",
                            message="Stream ended before the tool call "
                            "completed; the partial tool call was discarded.",
                            retryable=False,
                        )

            except httpx.TimeoutException as e:
                log.warning(
                    "provider stream timed out",
                    extra={"extra_fields": {"error": str(e)}},
                )
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
                log.warning(
                    "provider stream failed",
                    extra={"extra_fields": {"error": str(e)}},
                )
                yield ProviderError(
                    code="request_error",
                    message=str(e),
                    retryable=True,
                )

        async for chunk in retry_stream(_stream_once, self.retry_config):
            yield chunk

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
        """Parse an error response from the API.

        For auth failures (401) and context-length errors (413) the message
        is replaced with actionable guidance.  ``Retry-After`` headers are
        parsed and attached to the returned ``ProviderError``.
        """
        status = response.status_code
        body_text = body or response.text

        # Extract provider message and code
        provider_msg = ""
        provider_code = ""
        try:
            data = response.json() if body is None else __import__("json").loads(body)
            if isinstance(data, dict) and "error" in data:
                err = data["error"]
                if isinstance(err, dict):
                    provider_msg = str(err.get("message", "") or "")
                    provider_code = str(err.get("code", "") or "")
        except (ValueError, KeyError, TypeError):
            pass

        if not provider_msg:
            provider_msg = body_text[:300]

        # Determine code: use provider code if meaningful, else map from status
        code = provider_code
        if not code or code.startswith("http_"):
            code = _STATUS_CODE_MAP.get(status, f"http_{status}")

        # Override for known status codes that carry specific semantics
        if status == 401:
            code = "auth_failed"
        elif status == 413:
            code = "context_length_exceeded"

        retryable = status in (429, 500, 502, 503, 504)
        retry_after = _parse_retry_after(response.headers.get("retry-after"))

        # Build actionable messages for known error types
        if status == 401 or code == "auth_failed":
            message = auth_failure_message()
        elif status == 413 or code == "context_length_exceeded":
            message = context_length_message(provider_msg)
        else:
            message = provider_msg

        return ProviderError(
            code=code,
            message=message,
            status_code=status,
            retryable=retryable,
            retry_after=retry_after,
        )

    def _parse_stream_chunk(self, raw: str) -> StreamChunk | None:
        """Parse a single SSE event from the streaming response.

        Args:
            raw: The raw SSE event data (without the ``data: `` prefix).

        Returns:
            A ``StreamChunk``, or ``None`` for the ``[DONE]`` signal or
            non-data events.

        Raises:
            ValueError: If the raw data is malformed JSON.
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
            raise ValueError(f"malformed stream event: {line[:200]!r}") from None

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
