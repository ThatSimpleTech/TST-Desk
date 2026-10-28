"""Async provider client for OpenAI-compatible chat completion APIs.

One async ``httpx`` client, configurable to any OpenAI-compatible endpoint
(OpenRouter, local vLLM, etc.) with no code change. Supports streaming,
tool/function calling, and configurable timeouts.

The client reads the API key from the OS keychain (never from config or
environment files) — see :mod:`tstd.keychain`.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol

import httpx
from httpx_sse import aconnect_sse

from .logging import get_logger

log = get_logger("tstd.provider")

# Maps HTTP status codes to friendly error names.
_STATUS_CODE_MAP: dict[int, str] = {
    400: "bad_request",
    401: "auth_failed",
    402: "insufficient_credits",
    403: "forbidden",
    404: "not_found",
    413: "context_length_exceeded",
    429: "rate_limited",
    500: "server_error",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "gateway_timeout",
}


def _coerce_reasoning_details(raw: Any) -> list[dict[str, Any]] | None:
    """Keep only object items from an OpenRouter ``reasoning_details`` array."""
    if not isinstance(raw, list) or not raw:
        return None
    items = [item for item in raw if isinstance(item, dict)]
    return items or None


def flatten_reasoning_details(details: list[dict[str, Any]]) -> str | None:
    """Turn OpenRouter detail objects into the string the UI already streams.

    ``reasoning.text`` / ``reasoning.summary`` become visible thinking.
    Encrypted-only details still yield a non-empty marker so the first-token
    watchdog (TD-1713) does not treat a thinking model as hung.
    """
    parts: list[str] = []
    for item in details:
        typ = str(item.get("type", ""))
        if typ.endswith(".text") or typ == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        elif typ.endswith(".summary") or typ == "summary":
            summary = item.get("summary")
            if isinstance(summary, str) and summary:
                parts.append(summary)
        elif typ.endswith(".encrypted"):
            parts.append("[REDACTED]")
    if parts:
        return "".join(parts)
    return "[REDACTED]" if details else None


def merge_reasoning_details(acc: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> None:
    """Reassemble streamed ``reasoning_details`` in chunk order.

    Same ``index`` extends ``text`` / ``summary`` so a split thought becomes
    one object. That assembled array is what the next request must echo.
    """
    for item in incoming:
        idx = item.get("index")
        if isinstance(idx, int):
            for existing in acc:
                if existing.get("index") == idx:
                    for key in ("text", "summary"):
                        add = item.get(key)
                        if isinstance(add, str) and add:
                            prev = existing.get(key)
                            existing[key] = prev + add if isinstance(prev, str) else add
                    for key, value in item.items():
                        if key not in existing:
                            existing[key] = value
                    break
            else:
                acc.append(dict(item))
        else:
            acc.append(dict(item))


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


MessageContent = str | list[dict[str, Any]]


def content_as_text(content: MessageContent | None) -> str:
    """Plain text for compaction, distill, and branch labels (TD-4705).

    Multimodal user turns keep prose in ``text`` parts; image parts are
    omitted here because summaries and memory never need the pixels.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "\n".join(parts)


def tool_definition_payload(tool: ToolDefinition) -> dict[str, Any]:
    """One ``tools`` entry exactly as the request body carries it.

    The token estimate (TD-5007) counts this same shape, so the two
    cannot drift apart.
    """
    function: dict[str, Any] = {}
    if tool.function is not None:
        function = {
            "name": tool.function.name,
            "description": tool.function.description,
            "parameters": tool.function.parameters,
        }
    return {"type": tool.type, "function": function}


@dataclass
class ChatMessage:
    """A single message in the chat conversation."""

    role: Literal["system", "user", "assistant", "tool"]
    content: MessageContent | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    # OpenRouter tool-loop echo (TD-1903). Opaque; pass back unmodified.
    reasoning_details: list[dict[str, Any]] | None = None
    # Plaintext thinking echoed back to llama.cpp / vLLM (TD-5174). Set only
    # for a tier with ``echo_reasoning``, on assistant tool-call steps.
    reasoning_content: str | None = None
    # A user message sent mid-turn that joined the running turn (TD-5112).
    # It belongs to that turn: compaction never cuts at it and in-turn
    # elision reaches past it. Kept with the conversation, never sent.
    steer: bool = False
    # A done-check's feedback (TD-5124): the check failed, so its output
    # went back to the model as this user message. Always ``steer`` too,
    # since it joins the running turn; unlike a steer, no ``user_turn``
    # showed it, so it is not one of the window's numbered user turns.
    # Kept with the conversation, never sent.
    check: bool = False


def _prefix_content(prefix: str, content: MessageContent | None) -> MessageContent:
    """*content* with *prefix* ahead of it, keeping image parts."""
    if content is None or content == "" or content == []:
        return prefix
    if isinstance(content, str):
        return f"{prefix}\n\n{content}"
    return [{"type": "text", "text": prefix}, *content]


def turn_start_index(messages: Sequence[ChatMessage]) -> int:
    """Index of the user message that started the running turn, or 0.

    A message sent mid-turn (``steer``) joined that turn, so it does not
    start one.
    """
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if message.role == "user" and not message.steer:
            return i
    return 0


def current_turn_reasoning(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    """*messages* with echoed ``reasoning_content`` kept only in the running turn.

    Interleaved-thinking models need their thinking back between the tool
    steps of one turn, not from earlier turns; DeepSeek's own template drops
    it there. Sending all of it grew every request by each step's thinking
    for the life of the session (TD-5177).
    """
    start = turn_start_index(messages)
    return [
        replace(m, reasoning_content=None) if i < start and m.reasoning_content else m
        for i, m in enumerate(messages)
    ]


def wire_messages(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    """The messages as the request body carries them (TD-5142).

    Strict local chat templates (Qwen 3.5 and later, Mistral) accept one
    system message, first, and raise on any other. llama.cpp answers that
    with a 500, so a session whose history has a second system message
    fails every request from then on. Compaction stores its summary that
    way (compaction.py), so the wire, not the stored conversation, fixes
    it up:

    - system messages before the first other message merge into one, in
      order, a blank line apart. An automatic or whole-history /compact
      summary sits there, right after the system prompt.
    - a later system message (a /compact "from here" summary) goes ahead
      of the next user message's own text, so it keeps its place in the
      story. With no user message before the next assistant or tool
      message, it joins the first system message instead: a user message
      there could split a tool call from its results.

    System messages carry text; image parts in one are dropped. A
    conversation without a stray system message comes back as the same
    messages, so the body (and the server's prompt cache) is unchanged.
    """
    if not any(m.role == "system" for m in messages[1:]):
        return list(messages)

    leading: list[str] = []
    out: list[ChatMessage] = []
    pending: list[str] = []
    started = False
    for message in messages:
        if message.role == "system":
            text = content_as_text(message.content)
            if not started:
                leading.append(text)
            else:
                pending.append(text)
            continue
        started = True
        if pending:
            prefix = "\n\n".join(t for t in pending if t)
            pending = []
            if message.role == "user":
                if prefix:
                    message = replace(message, content=_prefix_content(prefix, message.content))
            else:
                leading.append(prefix)
        out.append(message)
    leading.extend(pending)

    if leading:
        out.insert(0, ChatMessage(role="system", content="\n\n".join(t for t in leading if t)))
    return out


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
    # Not sent. False returns a timed-out attempt instead of resending it
    # (TD-5146): an off-turn call that already waited a full read timeout
    # holds the session busy for another one on every retry.
    retry_on_timeout: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the OpenAI-compatible JSON request body."""
        d: dict[str, Any] = {
            "model": self.model,
            "messages": [
                self._message_to_dict(m)
                for m in wire_messages(current_turn_reasoning(self.messages))
            ],
        }
        if self.tools:
            d["tools"] = [tool_definition_payload(t) for t in self.tools]
            # OpenAI's default when tools are present. Some gateways (the GPU server
            # LiteLLM guided_json) treat an omitted tool_choice as "JSON in
            # content" and never fill message.tool_calls (TD-1724).
            d["tool_choice"] = "auto"
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
            # A tier's extra_body only fills in (TD-5008): what the caller
            # set, such as the classifier's temperature 0.0, wins.
            for key, value in self.extra_body.items():
                d.setdefault(key, value)
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
        if msg.reasoning_details:
            d["reasoning_details"] = msg.reasoning_details
        if msg.reasoning_content:
            d["reasoning_content"] = msg.reasoning_content
        return d


@dataclass
class Usage:
    """Token usage returned by the API.

    ``cached_prompt_tokens`` is ``None`` when the response carried no
    cached-token figure at all, and an integer — including ``0`` — when it
    did (TD-1811).  Silence is not a report of zero reuse: Ollama's
    OpenAI-compatible endpoint omits ``prompt_tokens_details`` entirely, so
    folding absent into ``0`` would let every downstream surface state a
    cache miss the provider never claimed.  ``None`` is unrepresentable as
    a token count, so ``mypy --strict`` makes each reader decide what
    "unknown" means instead of silently inheriting a fabricated zero.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int | None = None
    total_tokens: int = 0

    @classmethod
    def from_api_dict(cls, data: dict[str, Any] | None) -> Usage:
        """Parse usage from an API response dict."""
        if not data:
            return cls()
        details = data.get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        return cls(
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
            cached_prompt_tokens=cached,
            total_tokens=data.get("total_tokens", 0),
        )


@dataclass
class Delta:
    """A streaming delta — a chunk of assistant output.

    ``reasoning`` is a reasoning model's visible scratchpad, carried
    separately from ``content`` because it is not part of the answer: it
    is shown to the user but must never be replayed to the provider as
    something the assistant said (TD-1901). ``reasoning_details`` is the
    OpenRouter array that *must* be echoed on the next assistant
    message after tools (TD-1903).
    """

    content: str | None = None
    reasoning: str | None = None
    reasoning_details: list[dict[str, Any]] | None = None
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
                reasoning_details=_coerce_reasoning_details(msg_data.get("reasoning_details")),
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
    detail: str = ""
    """Upstream body, for logs only. Never copy this into assistant text."""
    endpoint_failed: bool = False
    """A reply cut off partway by the endpoint, not the request (TD-5120).

    Set on a ``stream_interrupted`` whose cause would be retried had
    nothing been shown: a dropped connection, or an in-band 429 or 5xx.
    It is not retried here, because the window shows part of the reply,
    but another endpoint can answer instead. Before output, ``retryable``
    says the same thing."""
    resendable: bool = False
    """A reply the endpoint cut off partway that the same endpoint may
    answer if asked again (TD-5183): a dropped connection, a stream that
    ended inside a tool call or without a finish, an in-band 429 or 5xx.
    A llama.cpp server that restarts mid-reply lands here. The turn
    withdraws what was shown and asks again (reply_retry.py). An in-band
    4xx is the request's fault and never set."""


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


def worst_case_retry_seconds(config: RetryConfig) -> float:
    """Upper bound on time spent sleeping between retries, in seconds.

    A caller that waits on a turn has to outlast the retries the daemon is
    entitled to make, or it reports a timeout for work that is still going —
    which is what a fixed client deadline does the moment ``max_retries`` is
    raised. Assumes no ``Retry-After`` header, since one can only be observed
    after the fact; a provider that sends a longer one can still overrun this.
    """
    total = 0.0
    for attempt in range(1, config.max_retries + 1):
        base: float = min(config.initial_delay * (2 ** (attempt - 1)), config.max_delay)
        total += base * (1 + config.jitter_factor)
    return total


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


def _should_retry(
    error: ProviderError,
    attempts: int,
    config: RetryConfig,
    retry_if: Callable[[ProviderError], bool] | None,
) -> bool:
    if not error.retryable or attempts >= config.max_retries:
        return False
    return retry_if is None or retry_if(error)


def _retry_filter(request: ChatCompletionRequest) -> Callable[[ProviderError], bool] | None:
    """The request's narrower retry test, or ``None`` for the full policy."""
    if request.retry_on_timeout:
        return None
    return _not_a_timeout


# A stall (TD-5106) is a timeout too: the socket's read timeout, the
# stream-idle watchdog or total_timeout ran out, and resending would make
# an off-turn call wait that long again (TD-5146).
_TIMEOUT_CODES = frozenset({"timeout", "stream_stalled"})


def _not_a_timeout(error: ProviderError) -> bool:
    return error.code not in _TIMEOUT_CODES


async def retry_call(
    call: Callable[[], Awaitable[ChatCompletionResponse | ProviderError]],
    config: RetryConfig,
    retry_if: Callable[[ProviderError], bool] | None = None,
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
    retry_if:
        Optional narrower test. A retryable error it rejects is returned
        as-is (TD-5146: a timed-out off-turn call is not resent).

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
        if not _should_retry(result, attempts, config, retry_if):
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
    retry_if: Callable[[ProviderError], bool] | None = None,
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
    retry_if:
        Optional narrower test, as for :func:`retry_call`.

    Yields
    ------
    ``StreamChunk`` for each delta (or ``ProviderError`` on failure).
    """
    attempts = 0
    while True:
        stream = open_stream()
        try:
            try:
                first = await anext(stream)
            except StopAsyncIteration:
                return

            if isinstance(first, ProviderError) and _should_retry(
                first, attempts, config, retry_if
            ):
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
                # Hang up before the wait: a stalled attempt still holds its
                # connection, and the server generates behind it (TD-5154).
                await aclose_stream(stream)
                await asyncio.sleep(delay)
                continue

            yield first
            async for item in stream:
                yield item
            return
        finally:
            # Every attempt is closed here, in order, the moment it is done
            # with: a stream yields its error from inside the response's
            # ``async with``, so one left to the garbage collector kept the
            # connection open and was closed by asyncio's finalizer, which
            # closes the nested generators in no fixed order (TD-5154).
            await aclose_stream(stream)


async def aclose_stream(stream: object) -> None:
    """Close an async generator now. A plain iterator has nothing to close."""
    close = getattr(stream, "aclose", None)
    if close is not None:
        await close()


class StreamingClient(Protocol):
    """The streaming half of the provider surface."""

    def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[StreamChunk | ProviderError]: ...


async def complete_streamed(
    provider: StreamingClient,
    request: ChatCompletionRequest,
) -> ChatCompletionResponse | ProviderError:
    """One completion over the streaming wire, folded into a response (TD-5146).

    For calls that want one answer rather than deltas: the work check and
    memory distill. A non-streamed request whose client hangs up keeps
    generating behind LiteLLM and holds a server slot; a streamed one is
    cancelled upstream within about a second. Streaming also makes the
    read timeout a gap between chunks instead of a cap on the whole answer.
    """
    stream = provider.chat_completion_stream(replace(request, stream=True))
    try:
        return await collect_stream(stream, model=request.model)
    finally:
        # Close it on an early return too; an abandoned generator is
        # otherwise closed only when it is collected.
        await aclose_stream(stream)


async def collect_stream(
    stream: AsyncIterator[StreamChunk | ProviderError],
    *,
    model: str,
) -> ChatCompletionResponse | ProviderError:
    """Fold a chunk stream into one response. The first error is returned.

    Plain ``reasoning`` deltas are dropped, as the non-streaming parser
    drops ``reasoning_content``: they are not the answer. A stream that
    ends without a finish reason was cut off, so it is an error, not a
    short answer: LiteLLM reports an upstream failure partway through as
    an in-band ``error`` event and then ends the body, and the partial
    text would otherwise read as a verdict.
    """
    response_id = ""
    content: list[str] = []
    details: list[dict[str, Any]] = []
    calls: dict[int, dict[str, str]] = {}
    finish_reason: str | None = None
    usage: Usage | None = None
    saw_chunk = False
    async for item in stream:
        if isinstance(item, ProviderError):
            return item
        saw_chunk = True
        response_id = response_id or item.id
        delta = item.delta
        if delta.content:
            content.append(delta.content)
        if delta.reasoning_details:
            merge_reasoning_details(details, delta.reasoning_details)
        for part in delta.tool_calls or ():
            entry = calls.setdefault(part.index, {"id": "", "name": "", "arguments": ""})
            if part.id:
                entry["id"] = part.id
            if part.function_name:
                entry["name"] = part.function_name
            if part.function_arguments:
                entry["arguments"] += part.function_arguments
        if item.usage is not None:
            usage = item.usage
        if item.finish_reason:
            finish_reason = item.finish_reason
    if not saw_chunk:
        return ProviderError(
            code="empty_stream",
            message="The provider closed the stream without sending anything.",
            retryable=False,
        )
    if finish_reason is None:
        return ProviderError(
            code="stream_interrupted",
            message="The stream ended before the answer finished; the partial "
            "answer was discarded.",
            retryable=False,
        )
    tool_calls = [
        ToolCall(id=c["id"], function=FunctionCall(name=c["name"], arguments=c["arguments"]))
        for _, c in sorted(calls.items())
    ]
    return ChatCompletionResponse(
        id=response_id,
        model=model,
        message=ChatMessage(
            role="assistant",
            content="".join(content) if content else None,
            tool_calls=tool_calls or None,
            reasoning_details=details or None,
        ),
        finish_reason=finish_reason,
        usage=usage,
    )


# ── Actionable error messages ─────────────────────────────────────────


def auth_failure_message() -> str:
    """Actionable guidance for authentication failures (HTTP 401)."""
    return (
        "Authentication failed. Fix one of these and retry: "
        "1) re-enter a valid API key for this provider — open the setup "
        "wizard from the gear in the title bar, "
        "2) check that the provider base_url in config.yaml "
        "matches the key's provider, "
        "3) verify the key has not expired or been revoked."
    )


# HTTP 400 bodies that mean "the prompt does not fit". Matched
# case-insensitively. 413 stays context_length_exceeded — that path
# already has its own copy, and a 5xx that quotes the same phrase is
# an outage, not a window problem.
_OVERFLOW_PHRASES: tuple[str, ...] = (
    "maximum context length",
    "context_length_exceeded",
    "contextwindowexceedederror",
    "prompt is too long",
    # llama.cpp: "request (N tokens) exceeds the available context size" (TD-5177).
    "exceeds the available context size",
    "exceed_context_size_error",
)


def _is_prompt_progress(raw: str) -> bool:
    """True for a llama.cpp ``prompt_progress`` event (TD-5183).

    A llama.cpp server asked for ``return_progress`` sends one per batch
    while it reads the prompt. On one GPU a cold 40k-token prompt takes
    ten minutes, longer than ``stream_idle_timeout``; these are what tell
    the watchdog the server is working rather than stuck.
    """
    if '"prompt_progress"' not in raw:
        return False
    try:
        data = __import__("json").loads(raw)
    except ValueError:
        return False
    return isinstance(data, dict) and isinstance(data.get("prompt_progress"), dict)


def _shows_nothing(chunk: StreamChunk) -> bool:
    """A chunk with no text, reasoning, tool call, finish or usage (TD-5158)."""
    delta = chunk.delta
    return not (
        delta.content
        or delta.reasoning
        or delta.reasoning_details
        or delta.tool_calls
        or chunk.finish_reason
        or chunk.usage is not None
    )


def is_context_overflow(status: int, body: str) -> bool:
    """True when an HTTP 400 body is a context-window refusal.

    OpenAI, vLLM, and LiteLLM do not share a code. They do share one of
    these phrases. Status is part of the match so a 413 keeps its own
    code and a 500 that echoes the phrase stays a server error.
    """
    if status != 400 or not body:
        return False
    folded = body.casefold()
    return any(phrase in folded for phrase in _OVERFLOW_PHRASES)


# A chat template that refuses the messages (TD-5142). The server could
# not build a prompt, so the same request fails the same way every time:
# llama.cpp reports "Jinja Exception: <reason>" as a 500, and Python
# servers name jinja2's exceptions. The reason stops at the end of its
# line or of the JSON string that quotes it.
_TEMPLATE_ERROR = re.compile(
    r"(?:Jinja Exception|jinja2\.exceptions\.\w+|TemplateError|TemplateSyntaxError)"
    r":[ \t]*([^\"\\\n]*)",
    re.IGNORECASE,
)

# Any other llama.cpp render failure (an unknown filter, a null operand)
# comes wrapped as "While executing <node> at line L, column C in source:
# ... Error: <reason>", with the newline raw or JSON-escaped.
_LLAMA_TEMPLATE_WRAPPER = re.compile(
    r"While executing \w+ at line \d+, column \d+ in source:.*?(?:\n|\\n)Error:"
    r"[ \t]*([^\"\\\n]*)",
    re.DOTALL,
)

# LiteLLM appends a failed fallback's error to the original one, which
# keeps its own status. Only the text before this says why the request
# failed; a template refusal from a fallback model must not stop the
# retries a 503 from the primary deserves.
_FALLBACK_TAIL = "Error doing the fallback:"


def template_error_reason(text: str) -> str | None:
    """The template's own reason when *text* reports a chat-template
    failure ("" when it gives none), else None."""
    own = text.split(_FALLBACK_TAIL, 1)[0]
    match = _TEMPLATE_ERROR.search(own) or _LLAMA_TEMPLATE_WRAPPER.search(own)
    if match is None:
        return None
    return match.group(1).strip()


def template_error_message(reason: str) -> str:
    """Plain copy for a chat-template refusal. Resending sends the same
    messages, so it says so instead of asking for a retry."""
    detail = f": {reason.rstrip('.')}." if reason else "."
    return (
        f"The model server's chat template refused this conversation{detail} "
        "Resending won't help. Start a fresh session, or switch the tier "
        "to another model in config.yaml."
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
    """Timeout configuration for API requests (per tier since TD-5106).

    All values are in seconds.  ``connect``, ``read``, ``write`` and
    ``pool`` go to httpx.  ``total`` and ``stream_idle`` are enforced here,
    because httpx has neither.
    """

    connect: float = 30.0
    """Timeout for establishing the connection."""

    read: float = 120.0
    """Longest wait for any bytes from the socket, headers included."""

    write: float = 30.0
    """Timeout for writing the request body."""

    pool: float = 180.0
    """Longest wait for a free pooled connection.  Before TD-5106 this was
    the field named ``total``, which never bounded the whole request."""

    total: float | None = None
    """Wall-clock cap on one attempt, streaming included.  None: no cap."""

    stream_idle: float | None = 300.0
    """Longest a stream may go without a usable chunk.  SSE keep-alive
    comments do not count, so a server that only pings is caught.  None:
    no watchdog (the socket ``read`` timeout still applies)."""

    def to_httpx_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect,
            read=self.read,
            write=self.write,
            pool=self.pool,
        )


# ── Provider client ────────────────────────────────────────────────────


class ProviderClient:
    """Async HTTP client for an OpenAI-compatible chat completion API.

    Usage::

        api_key = "sk-your-key-here"  # loaded from keychain via from_keychain()
        client = ProviderClient(
            base_url="<your-provider-endpoint>",  # comes from config.yaml, never hardcoded
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
        api_key: str | None,
        timeout: TimeoutConfig | None = None,
        client: httpx.AsyncClient | None = None,
        retry_config: RetryConfig | None = None,
    ) -> None:
        """Build a client for *base_url*.

        *api_key* is required positionally but may be ``None`` for an endpoint
        that takes no credential — a loopback model server (TD-1801). ``None``
        omits the ``Authorization`` header entirely rather than sending an
        empty bearer, so "keyless" is provable on the wire.
        """
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
                async with asyncio.timeout(self.timeout.total):
                    response = await self._client.post(
                        f"{self.base_url}/chat/completions",
                        json=body,
                        headers=self._headers(),
                    )
            except TimeoutError:
                log.warning("provider request passed total_timeout")
                return ProviderError(
                    code="timeout",
                    message=f"The reply took longer than total_timeout ({self.timeout.total:g} s).",
                    retryable=True,
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
                    embedded = self._error_envelope_status(data)
                    if embedded is not None:
                        return self._parse_error(response, status_override=embedded)
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

        return await retry_call(_attempt, self.retry_config, _retry_filter(request))

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
        a half-parsed tool call.  So does a stream that ends with neither
        a finish reason nor ``[DONE]`` (TD-5152): a gateway that gave up
        partway, whose text so far is not an answer.  An in-band
        ``{"error": ...}`` event (with or without ``choices``) or a choice
        that finishes with ``"error"`` is mapped as an HTTP error with its
        status before any chunk, and is ``stream_interrupted`` after one.

        Args:
            request: The chat completion request.

        Yields:
            ``StreamChunk`` for each delta, or ``ProviderError`` on failure.
        """
        body = request.to_dict()
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}

        async def _stream_once() -> AsyncIterator[StreamChunk | ProviderError]:
            started = asyncio.get_running_loop().time()
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

                    # A 2xx that is JSON rather than an event stream is an
                    # error envelope wearing a success status — there are no
                    # SSE events to read, so letting it through would end the
                    # turn as an empty completion instead of a retryable
                    # failure.
                    if "text/event-stream" not in sse.response.headers.get("content-type", ""):
                        raw = await sse.response.aread()
                        text = raw.decode() if raw else ""
                        try:
                            data = __import__("json").loads(text) if text else None
                        except ValueError:
                            data = None
                        embedded = self._error_envelope_status(data)
                        if embedded is not None:
                            yield self._parse_error(
                                sse.response, text or None, status_override=embedded
                            )
                            return

                    in_tool_call = False
                    saw_finish = False
                    saw_done = False
                    yielded_any = False
                    # Chunks that show nothing (the usual opening
                    # ``{"role": "assistant", "content": ""}``), held until
                    # one that does. Until then nothing is on screen, so an
                    # error event is the status it names and retried as
                    # one (TD-5158), and ``retry_stream`` sees it first.
                    held: list[StreamChunk] = []
                    events = sse.aiter_sse()
                    loop = asyncio.get_running_loop()
                    total_deadline = (
                        None if self.timeout.total is None else started + self.timeout.total
                    )
                    idle_deadline = (
                        None
                        if self.timeout.stream_idle is None
                        else loop.time() + self.timeout.stream_idle
                    )
                    try:
                        while True:
                            deadlines = [
                                d for d in (idle_deadline, total_deadline) if d is not None
                            ]
                            try:
                                async with asyncio.timeout_at(min(deadlines, default=None)):
                                    event = await anext(events)
                            except StopAsyncIteration:
                                break
                            except TimeoutError:
                                yield self._stall_error(
                                    total_deadline is not None and loop.time() >= total_deadline,
                                    yielded_any,
                                )
                                return
                            if event.data.strip() == "[DONE]":
                                # The reply is over. Reading on would let the
                                # idle watchdog or total_timeout fail a finished
                                # reply if the server holds the body open.
                                saw_done = True
                                break
                            inband = self._inband_error(event.data)
                            if inband is not None:
                                yield self._inband_stream_error(
                                    sse.response, inband[0], inband[1], yielded_any
                                )
                                return
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
                            if idle_deadline is not None and self.timeout.stream_idle:
                                idle_deadline = loop.time() + self.timeout.stream_idle
                            if not yielded_any and _shows_nothing(chunk):
                                # llama.cpp's ``prompt_progress`` while it reads
                                # a long prompt (TD-5183): a sign of life for
                                # the watchdog above, never part of the reply.
                                if not _is_prompt_progress(event.data):
                                    held.append(chunk)
                                continue
                            if chunk.delta.tool_calls:
                                in_tool_call = True
                            if chunk.finish_reason:
                                saw_finish = True
                            yielded_any = True
                            for early in held:
                                yield early
                            held.clear()
                            yield chunk
                    except httpx.ReadTimeout:
                        # The socket went quiet for ``read`` seconds: the
                        # same stall the watchdog catches, one layer down.
                        yield self._stall_error(False, yielded_any, socket=True)
                        return
                    except httpx.HTTPError as e:
                        yield self._dropped_error(e, yielded_any, in_tool_call and not saw_finish)
                        return

                    if held:
                        # The stream ended on chunks that showed nothing.
                        # They are still the reply, as before.
                        yielded_any = True
                        for early in held:
                            yield early
                        held.clear()
                    if in_tool_call and not saw_finish:
                        yield ProviderError(
                            code="stream_interrupted",
                            message="Stream ended before the tool call "
                            "completed; the partial tool call was discarded.",
                            retryable=False,
                            resendable=True,
                        )
                    elif not yielded_any:
                        # A 200 event-stream that carries no events at all.
                        # Yielding nothing here ends the generator, which
                        # ``retry_stream`` cannot tell from a finished stream —
                        # so the turn reports an empty completion and blames
                        # the model for the provider closing on it. Say what
                        # happened, and let it retry.
                        log.warning("provider stream carried no events")
                        yield ProviderError(
                            code="empty_stream",
                            message="The provider accepted the request and then "
                            "closed the stream without sending anything. Nothing "
                            "was generated. Resend to retry.",
                            retryable=True,
                        )
                    elif not saw_finish and not saw_done:
                        # TD-5152: the body ended with neither a finish
                        # reason nor ``[DONE]``. The server or a gateway gave
                        # up partway; the text so far is not an answer.
                        log.warning("provider stream ended without a finish")
                        yield ProviderError(
                            code="stream_interrupted",
                            message="The provider's stream ended before the reply "
                            "finished; the partial reply was discarded. Resend to retry.",
                            retryable=False,
                            resendable=True,
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

        attempts = retry_stream(_stream_once, self.retry_config, _retry_filter(request))
        try:
            async for chunk in attempts:
                yield chunk
        finally:
            # ``async for`` does not close what it iterates. A consumer that
            # stops early (an error handed on, Stop) closes this generator,
            # and the hang-up has to reach the attempt and its response now.
            await aclose_stream(attempts)

    def _dropped_error(
        self, error: httpx.HTTPError, yielded_any: bool, in_tool_call: bool
    ) -> ProviderError:
        """The error for a connection that dropped inside a ``200`` stream.

        Nothing shown yet: the same transport failure as a drop before the
        headers (``request_error``), retried under ``provider_retry``. A
        llama.cpp or vLLM restart while it reads a long prompt lands here,
        and nothing on screen stands in the way of asking again (TD-5120).
        Part of a reply shown: ``stream_interrupted``, not retried, because
        a second attempt would repeat text the window already has; it is
        marked ``endpoint_failed`` so the turn can move to a backup.
        """
        log.warning(
            "provider stream dropped",
            extra={"extra_fields": {"error": str(error), "after_output": yielded_any}},
        )
        if not yielded_any:
            return ProviderError(
                code="request_error",
                message=f"The connection to {self.base_url} dropped before the reply "
                f"began: {error}",
                retryable=True,
            )
        word = "tool call" if in_tool_call else "response"
        return ProviderError(
            code="stream_interrupted",
            message=f"Stream interrupted mid-{word}: {error}. Any partial content was discarded.",
            retryable=False,
            endpoint_failed=True,
            resendable=True,
        )

    def _stall_error(
        self, past_total: bool, yielded_any: bool, *, socket: bool = False
    ) -> ProviderError:
        """The error for a stream that went quiet or ran too long (TD-5106).

        Nothing shown yet: retryable, so ``retry_stream`` tries again under
        ``provider_retry``.  Part of a reply shown: not retryable here,
        because a second attempt would repeat text the window already has.
        The turn withdraws that text and asks once more itself (TD-5159,
        reply_retry.py); the off-turn calls return it.
        """
        if past_total:
            what = f"The reply took longer than total_timeout ({self.timeout.total:g} s)."
            code = "timeout"
        else:
            quiet = self.timeout.read if socket else self.timeout.stream_idle
            key = "read_timeout" if socket else "stream_idle_timeout"
            where = "partway through the reply" if yielded_any else "after accepting the request"
            what = f"The provider sent nothing for {quiet:g} s {where} ({key})."
            code = "stream_stalled"
        log.warning(
            "provider stream stalled",
            extra={"extra_fields": {"code": code, "after_output": yielded_any}},
        )
        if yielded_any:
            return ProviderError(
                code=code,
                message=f"{what} The partial reply was discarded. Resend to retry.",
                retryable=False,
            )
        return ProviderError(
            code=code,
            message=f"{what} Raise it for a slow local model, or resend.",
            retryable=True,
        )

    @classmethod
    def _inband_error(cls, raw: str) -> tuple[str, int] | None:
        """An SSE event that is an error, as ``(error JSON, status)`` (TD-5152).

        Two shapes are in the wild, and either part alone is enough:

        - LiteLLM reports an upstream failure partway through a stream as
          ``data: {"error": {...}}`` and then ends the body. The event has
          no ``choices``, so the chunk parser reads it as nothing at all.
        - OpenRouter sends a top-level ``error`` beside a choice with an
          empty delta and ``finish_reason: "error"``. The chunk parser
          reads that as a finished reply, so the cut-off text would stand.

        A choice that finishes with ``"error"`` and no ``error`` object is
        an error too, with no status of its own (a 502) and a short reason
        in place of a message, so the window never shows raw JSON.
        """
        if '"error"' not in raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        error = data.get("error")
        choices = data.get("choices")
        ended_in_error = isinstance(choices, list) and any(
            isinstance(c, dict) and c.get("finish_reason") == "error" for c in choices
        )
        if error:
            status = cls._error_envelope_status({"error": error})
            return raw.strip(), 502 if status is None else status
        if ended_in_error:
            body = {
                "error": {"message": 'finish_reason "error" with no message'},
                "event": data,
            }
            return json.dumps(body), 502
        return None

    def _inband_stream_error(
        self, response: httpx.Response, raw: str, status: int, yielded_any: bool
    ) -> ProviderError:
        """The error an in-band event stands for, mapped as its status would be.

        Nothing shown yet: the same error an HTTP reply with that status
        gives, so a 429 or 5xx is retried under ``provider_retry``. Part of
        a reply shown: ``stream_interrupted``, never retried, because a
        second attempt would repeat text the window already has. A 429 or
        5xx there is still the endpoint's failure (``endpoint_failed``), so
        the turn can move to a backup (TD-5120).
        """
        error = self._parse_error(response, raw, status_override=status)
        log.warning(
            "provider stream carried an error event",
            extra={
                "extra_fields": {
                    "code": error.code,
                    "status_code": error.status_code,
                    "after_output": yielded_any,
                }
            },
        )
        if not yielded_any:
            return error
        reason = error.message.strip()
        # An event with no message of its own leaves the raw JSON here; the
        # chat would then show a generic rejection instead of this sentence.
        if reason.startswith(("{", "[")) or '{"' in reason:
            reason = ""
        reason = f" ({reason.rstrip('.')})" if reason else ""
        return ProviderError(
            code="stream_interrupted",
            message=f"The provider reported an error partway through the reply{reason}. "
            "The partial reply was discarded. Resend to retry.",
            status_code=error.status_code,
            retryable=False,
            detail=error.detail,
            endpoint_failed=error.retryable,
            resendable=error.retryable,
        )

    def _headers(self) -> dict[str, str]:
        """Build the request headers.

        Without a key the ``Authorization`` header is absent rather than an
        empty bearer: a strict OpenAI-compatible server would reject the
        malformed value, and an absent header is the honest statement.
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key is not None:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _error_envelope_status(data: object) -> int | None:
        """The HTTP-ish status inside a 2xx body that is really an error.

        OpenRouter can answer an upstream refusal with ``200 OK`` whose body
        is ``{"error": {"message": ..., "code": 429}}`` — no ``choices`` at
        all. Trusting the status line then loses the failure twice over: the
        non-streaming path raises ``KeyError`` on ``choices`` and reports an
        unretryable ``parse_error``, and the streaming path finds no SSE
        events and reports an empty completion. Both are wrong and neither
        retries, which is how a turn that only needed a moment's patience
        ends as a dead end.

        Returns the embedded status, or ``None`` when *data* is a normal
        response.
        """
        if not isinstance(data, dict) or "choices" in data:
            return None
        err = data.get("error")
        if not isinstance(err, dict):
            return None
        code = err.get("code")
        status: int | None = None
        if isinstance(code, int) and not isinstance(code, bool):  # a bool is no status
            status = code
        elif isinstance(code, str) and code.lstrip("-").isdigit():
            status = int(code)
        # Only an error status is one. A 0, a negative or a 2xx would be
        # classified as the 200 the stream claimed and never retried (TD-5158).
        if status is not None and 400 <= status <= 599:
            return status
        # A window refusal named by a string code (OpenAI's
        # ``context_length_exceeded``) or only in its message is the 400 an
        # HTTP reply gives it, so it becomes ``context_overflow`` and the
        # loop's tighter-window retry runs instead of resending (TD-5158).
        named = f"{code if isinstance(code, str) else ''} {err.get('message') or ''}"
        if is_context_overflow(400, named):
            return 400
        # An error envelope with no usable status is still an error.
        return 502

    def _parse_error(
        self,
        response: httpx.Response,
        body: str | None = None,
        status_override: int | None = None,
    ) -> ProviderError:
        """Parse an error response from the API.

        For auth failures (401) and context-length errors (413) the message
        is replaced with actionable guidance.  ``Retry-After`` headers are
        parsed and attached to the returned ``ProviderError``.

        ``status_override`` classifies a body that carries its own status —
        an error envelope delivered under a 2xx status line — so it is
        treated as the failure it is rather than the success it claims.
        """
        status = status_override or response.status_code
        body_text = body or response.text

        # Extract provider message and code
        provider_msg = ""
        provider_code = ""
        provider_hint = ""
        try:
            data = response.json() if body is None else __import__("json").loads(body)
            if isinstance(data, dict) and "error" in data:
                err = data["error"]
                if isinstance(err, dict):
                    provider_msg = str(err.get("message", "") or "")
                    provider_code = str(err.get("code", "") or "")
                    # A gateway's own ``message`` is often a placeholder —
                    # OpenRouter sends the literal "Provider returned error"
                    # while the upstream reason ("temporarily rate-limited
                    # upstream. Please retry shortly.") and the way out sit in
                    # ``metadata``. Dropping those is how a turn ends up
                    # reporting nothing the user can act on.
                    meta = err.get("metadata")
                    if isinstance(meta, dict):
                        raw = str(meta.get("raw", "") or "").strip()
                        if raw:
                            provider_msg = raw
                        provider_hint = str(meta.get("remedy_hint", "") or "").strip()
        except (ValueError, KeyError, TypeError):
            pass

        if not provider_msg:
            provider_msg = body_text[:300]

        # Determine code: use provider code if meaningful, else map from status.
        # A purely numeric provider code restates the HTTP status rather than
        # naming a cause — OpenRouter sends {"code": 429} alongside its 429 —
        # and taking it verbatim shadows the map below, stranding every
        # consumer on the unknown-code path for the most common failures.
        code = provider_code
        if not code or code.startswith("http_") or code.lstrip("-").isdigit():
            code = _STATUS_CODE_MAP.get(status, f"http_{status}")

        # A 400 whose body is a window refusal is its own code, checked
        # before the 413 override. Gateways label the same refusal
        # context_length_exceeded on HTTP 400; that must not pick up the
        # 413 copy, which appends the raw provider text.
        if is_context_overflow(status, body_text) or is_context_overflow(status, provider_msg):
            code = "context_overflow"

        template_reason = template_error_reason(provider_msg)
        if template_reason is None:
            template_reason = template_error_reason(body_text)
        if template_reason is not None and code != "context_overflow" and status not in (401, 413):
            code = "template_error"

        # Override for known status codes that carry specific semantics
        if status == 401:
            code = "auth_failed"
        elif status == 413:
            code = "context_length_exceeded"

        retryable = status in (429, 500, 502, 503, 504) and code != "template_error"
        retry_after = _parse_retry_after(response.headers.get("retry-after"))

        # Build actionable messages for known error types. Overflow copy
        # is one sentence — the raw body stays on ``detail`` for the log.
        if code == "context_overflow":
            message = "The request exceeded the model's context window."
        elif code == "template_error":
            message = template_error_message(template_reason or "")
        elif status == 401 or code == "auth_failed":
            message = auth_failure_message()
        elif status == 413 or code == "context_length_exceeded":
            message = context_length_message(provider_msg)
        else:
            # The gateway's remedy hint names the way out (add a BYOK key,
            # route to another provider). It is the only actionable half of
            # an upstream-capacity refusal, so it travels with the reason.
            message = f"{provider_msg} {provider_hint}".strip() if provider_hint else provider_msg

        return ProviderError(
            code=code,
            message=message,
            status_code=status,
            retryable=retryable,
            retry_after=retry_after,
            detail=body_text,
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

        # Reasoning delta (TD-1901 / TD-1903).  Three spellings are in
        # the wild and none is in the OpenAI schema: Ollama emits
        # ``reasoning``, DeepSeek emits ``reasoning_content``, OpenRouter
        # streams ``reasoning_details`` (array).  A string field wins for
        # the UI when present; details still ride along for the tool-loop
        # echo.  Empty strings normalise to None so a chunk carrying
        # nothing but ``content: ""`` cannot be mistaken for thinking.
        reasoning_raw = delta_data.get("reasoning") or delta_data.get("reasoning_content")
        reasoning = reasoning_raw if isinstance(reasoning_raw, str) and reasoning_raw else None
        reasoning_details = _coerce_reasoning_details(delta_data.get("reasoning_details"))
        if reasoning is None and reasoning_details:
            reasoning = flatten_reasoning_details(reasoning_details)

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
            delta=Delta(
                content=content,
                reasoning=reasoning,
                reasoning_details=reasoning_details,
                tool_calls=tool_calls,
            ),
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
        retry_config: RetryConfig | None = None,
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
            retry_config=retry_config,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client and release resources."""
        await self._client.aclose()

    async def __aenter__(self) -> ProviderClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
