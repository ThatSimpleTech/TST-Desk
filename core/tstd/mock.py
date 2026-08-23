"""Mock provider for offline, deterministic testing.

Implements the same interface as :class:`tstd.provider.ProviderClient`
(``chat_completion`` / ``chat_completion_stream``) but never touches the
network. Responses are scripted per model name so loop and router tests can
exercise every failure mode without spend.

Used by every loop and router test (AGENTS.md §7).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from .provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Delta,
    DeltaToolCall,
    FunctionCall,
    ProviderError,
    StreamChunk,
    ToolCall,
    Usage,
)

# Default realistic usage numbers so cost tests are meaningful (TD-304).
DEFAULT_PROMPT_TOKENS = 1_200
DEFAULT_COMPLETION_TOKENS = 480
DEFAULT_CACHED_TOKENS = 1_000


@dataclass
class Script:
    """A scripted response for a given model name.

    Attributes:
        kind: What the mock should produce.
        content: Text for ``text`` and ``stream`` kinds, or an error message.
        tool_name: Function name for the ``tool_call`` kind.
        tool_arguments: JSON string of arguments for the ``tool_call`` kind.
        status_code: HTTP status for ``error`` / ``rate_limit`` kinds.
        error_code: Typed error code for the ``error`` kind.
        fail_times: If > 0, the first N calls return an error (rate-limit by
            default, or the kind's normal error) before the script's real
            response.
        retry_after: ``Retry-After`` value (seconds) attached to error
            responses when ``fail_times`` applies.
        chunk_delay: Seconds to ``await asyncio.sleep()`` between stream
            chunks.  Used by cancellation tests so the mock doesn't complete
            before the test can cancel.
        prompt_tokens / completion_tokens: usage figures.
        cached_tokens: cached-prompt-token figure to report, or ``None``
            to script a provider that reports no cache figure at all —
            what Ollama's OpenAI-compatible endpoint does (TD-1811).
        reasoning: Thinking emitted before ``content`` on the ``stream``
            kind, word by word, each chunk carrying ``content=""`` beside
            it — the shape Ollama actually sends for a reasoning model
            (TD-1901).  Empty by default, so every existing script is a
            non-reasoning provider and stays byte-identical.
        reasoning_details: OpenRouter array echoed on the next assistant
            message after tools (TD-1903). Empty by default.
    """

    kind: Literal[
        "text", "stream", "tool_call", "malformed", "error", "rate_limit", "stream_interrupted"
    ]
    content: str = ""
    reasoning: str = ""
    reasoning_details: list[dict[str, Any]] = field(default_factory=list)
    tool_name: str = ""
    tool_arguments: str = ""
    status_code: int = 0
    error_code: str = ""
    fail_times: int = 0
    retry_after: float | None = None
    chunk_delay: float = 0.0
    prompt_tokens: int = DEFAULT_PROMPT_TOKENS
    completion_tokens: int = DEFAULT_COMPLETION_TOKENS
    cached_tokens: int | None = DEFAULT_CACHED_TOKENS

    @property
    def usage(self) -> Usage:
        """A usage object with this script's token counts."""
        return Usage(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cached_prompt_tokens=self.cached_tokens,
            total_tokens=self.prompt_tokens + self.completion_tokens,
        )


class MockProvider:
    """In-process provider that returns scripted responses.

    Scripts are keyed by model name. An optional default script handles any
    model without an explicit script.  An optional sequence of scripts per
    model plays one script per call in order, then falls back to the
    regular script/default (useful for tool-call round-trip tests).

    Usage::

        mock = MockProvider(
            scripts={"echo": Script(kind="text", content="Hello!")},
            default=Script(kind="stream", content="Default reply"),
        )
        response = await mock.chat_completion(ChatCompletionRequest(...))

        # Deterministic multi-call sequence:
        mock = MockProvider(
            sequences={
                "brain": [
                    Script(kind="tool_call", tool_name="echo", tool_arguments='{"m": "x"}'),
                    Script(kind="stream", content="Done"),
                ]
            }
        )
    """

    def __init__(
        self,
        scripts: Mapping[str, Script] | None = None,
        default: Script | None = None,
        sequences: Mapping[str, list[Script]] | None = None,
    ) -> None:
        self._scripts: dict[str, Script] = dict(scripts or {})
        self._default = default or Script(kind="text", content="Hello from mock provider")
        self._sequences: dict[str, list[Script]] = dict(sequences or {})
        self._seq_pos: dict[str, int] = {}
        # Every request is recorded so tests can assert what was sent.
        self.calls: list[ChatCompletionRequest] = []
        # Per-model call counter for fail_times support.
        self._fail_counts: dict[str, int] = {}

    def script(self, model: str, script: Script) -> None:
        """Register a script for a model name."""
        self._scripts[model] = script

    def _script_for(self, model: str) -> Script:
        # A sequence plays one script per call, in order.
        seq = self._sequences.get(model)
        if seq is not None:
            pos = self._seq_pos.get(model, 0)
            if pos < len(seq):
                self._seq_pos[model] = pos + 1
                return seq[pos]
        return self._scripts.get(model, self._default)

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse | ProviderError:
        """Return a scripted non-streaming response."""
        self.calls.append(request)
        script = self._script_for(request.model)
        if self._should_fail(request.model, script):
            return self._fail_error(script)
        return self._render_nonstream(script, request.model)

    async def chat_completion_stream(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[StreamChunk | ProviderError]:
        """Yield a scripted streaming response."""
        self.calls.append(request)
        script = self._script_for(request.model)

        if self._should_fail(request.model, script):
            yield self._fail_error(script)
            return

        if script.kind in ("error", "rate_limit"):
            yield self._render_error(script)
            return

        async for chunk in self._render_stream(script, request.model):
            yield chunk

    # ── Failure simulation ──────────────────────────────────────────

    def _should_fail(self, model: str, script: Script) -> bool:
        """Return True if the mock should return an error for this call.

        When ``script.fail_times > 0``, the first N calls to *model*
        return a retryable error.  After N calls the real script response
        is produced.
        """
        if script.fail_times <= 0:
            return False
        count = self._fail_counts.get(model, 0)
        self._fail_counts[model] = count + 1
        return count < script.fail_times

    def _fail_error(self, script: Script) -> ProviderError:
        """Error returned while a script is in its ``fail_times`` window."""
        status = script.status_code or 429
        retryable = status in (429, 500, 502, 503, 504)
        code = script.error_code or ("rate_limited" if status == 429 else "server_error")
        return ProviderError(
            code=code,
            message=script.content or f"Simulated failure (HTTP {status})",
            status_code=status,
            retryable=retryable,
            retry_after=script.retry_after,
        )

    # ── Rendering ────────────────────────────────────────────────────

    def _render_nonstream(
        self, script: Script, model: str
    ) -> ChatCompletionResponse | ProviderError:
        if script.kind == "error":
            return self._render_error(script)
        if script.kind == "rate_limit":
            return self._render_error(script)
        if script.kind == "stream_interrupted":
            return ProviderError(
                code="stream_interrupted",
                message="Stream interrupted mid-tool-call (mock)",
                status_code=200,
                retryable=False,
            )
        if script.kind == "malformed":
            return ProviderError(
                code="parse_error",
                message="Malformed response from mock provider",
                status_code=200,
                retryable=False,
            )

        if script.kind == "tool_call":
            message = ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_mock_1",
                        type="function",
                        function=FunctionCall(
                            name=script.tool_name or "mock_tool",
                            arguments=script.tool_arguments or "{}",
                        ),
                    )
                ],
            )
            finish_reason = "tool_calls"
        else:
            message = ChatMessage(role="assistant", content=script.content)
            finish_reason = "stop"

        return ChatCompletionResponse(
            id=f"mock-{model}",
            model=model,
            message=message,
            finish_reason=finish_reason,
            usage=script.usage,
        )

    def _render_error(self, script: Script) -> ProviderError:
        if script.kind == "rate_limit":
            return ProviderError(
                code="rate_limited",
                message=script.content or "Rate limit exceeded",
                status_code=429,
                retryable=True,
                retry_after=script.retry_after,
            )
        return ProviderError(
            code=script.error_code or "server_error",
            message=script.content or "Mock provider error",
            status_code=script.status_code or 500,
            retryable=script.status_code in (429, 500, 502, 503, 504),
            retry_after=script.retry_after,
        )

    async def _render_stream(self, script: Script, model: str) -> AsyncIterator[StreamChunk]:
        chunk_id = f"mock-stream-{model}"

        # Helper: optional delay between chunks for cancellation tests
        async def _maybe_delay() -> None:
            if script.chunk_delay > 0:
                await asyncio.sleep(script.chunk_delay)

        if script.kind == "stream_interrupted":
            # Emit tool-call deltas then stop WITHOUT finish_reason (clean
            # close).  Simulates a provider that drops the connection mid-tool-call.
            yield StreamChunk(
                id=chunk_id,
                delta=Delta(
                    content=None,
                    tool_calls=[
                        DeltaToolCall(
                            index=0,
                            id="call_interrupted_1",
                            function_name="mock_tool",
                            function_arguments="",
                        )
                    ],
                ),
                finish_reason=None,
            )
            yield StreamChunk(
                id=chunk_id,
                delta=Delta(
                    content=None,
                    tool_calls=[
                        DeltaToolCall(
                            index=0,
                            function_name=None,
                            function_arguments='{"partial": "true", "loc": "Sa',
                        )
                    ],
                ),
                finish_reason=None,
            )
            # No finish chunk, no usage — stream ends abruptly
            return

        if script.kind == "tool_call":
            if script.reasoning_details:
                await _maybe_delay()
                yield StreamChunk(
                    id=chunk_id,
                    delta=Delta(
                        content="",
                        reasoning_details=list(script.reasoning_details),
                    ),
                    finish_reason=None,
                )
            # Emit role chunk, then arguments, then finish chunk with usage.
            await _maybe_delay()
            yield StreamChunk(
                id=chunk_id,
                delta=Delta(
                    content=None,
                    tool_calls=[
                        self._tool_delta(
                            index=0,
                            id="call_mock_1",
                            name=script.tool_name or "mock_tool",
                            arguments="",
                        )
                    ],
                ),
                finish_reason=None,
            )
            await _maybe_delay()
            yield StreamChunk(
                id=chunk_id,
                delta=Delta(
                    content=None,
                    tool_calls=[
                        self._tool_delta(
                            index=0,
                            name=None,
                            arguments=script.tool_arguments or "{}",
                        )
                    ],
                ),
                finish_reason=None,
            )
            await _maybe_delay()
            yield StreamChunk(
                id=chunk_id,
                delta=Delta(),
                finish_reason="tool_calls",
                usage=script.usage,
            )
            return

        # Reasoning first, and with content="" on every chunk (TD-1901):
        # a mock that left content None would not reproduce the defect,
        # since the falsy check the loop performs treats both alike but
        # only the empty string is what the real provider sends.
        reasoning_words = script.reasoning.split(" ") if script.reasoning else []
        details = list(script.reasoning_details) if script.reasoning_details else None
        if details and not reasoning_words:
            await _maybe_delay()
            yield StreamChunk(
                id=chunk_id,
                delta=Delta(content="", reasoning_details=details),
                finish_reason=None,
            )
        for i, word in enumerate(reasoning_words):
            await _maybe_delay()
            yield StreamChunk(
                id=chunk_id,
                # Separators ride on the following chunk, as they do for
                # content below, so concatenating the deltas reproduces the
                # script exactly rather than running the words together.
                delta=Delta(
                    content="",
                    reasoning=word if i == 0 else f" {word}",
                    reasoning_details=details if i == 0 else None,
                ),
                finish_reason=None,
            )

        # Plain text: split content into word-sized deltas so consumers must
        # accumulate deltas rather than assume one chunk per response.
        words = script.content.split(" ") if script.content else []
        if words:
            await _maybe_delay()
            yield StreamChunk(
                id=chunk_id,
                delta=Delta(content=" ".join(words[:1])),
                finish_reason=None,
            )
            for i in range(1, len(words)):
                await _maybe_delay()
                yield StreamChunk(
                    id=chunk_id,
                    delta=Delta(content=f" {words[i]}" if i > 0 else words[i]),
                    finish_reason=None,
                )
        await _maybe_delay()
        yield StreamChunk(
            id=chunk_id,
            delta=Delta(),
            finish_reason="stop",
            usage=script.usage,
        )

    @staticmethod
    def _tool_delta(
        *,
        index: int,
        id: str | None = None,
        name: str | None = None,
        arguments: str,
    ) -> DeltaToolCall:
        """Build a DeltaToolCall for streaming tool call chunks."""
        return DeltaToolCall(
            index=index,
            id=id,
            function_name=name,
            function_arguments=arguments,
        )
