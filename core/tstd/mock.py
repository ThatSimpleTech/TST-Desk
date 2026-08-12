"""Mock provider for offline, deterministic testing.

Implements the same interface as :class:`tstd.provider.ProviderClient`
(``chat_completion`` / ``chat_completion_stream``) but never touches the
network. Responses are scripted per model name so loop and router tests can
exercise every failure mode without spend.

Used by every loop and router test (AGENTS.md §7).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Literal

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
        prompt_tokens / completion_tokens / cached_tokens: usage figures.
    """

    kind: Literal["text", "stream", "tool_call", "malformed", "error", "rate_limit"]
    content: str = ""
    tool_name: str = ""
    tool_arguments: str = ""
    status_code: int = 0
    error_code: str = ""
    prompt_tokens: int = DEFAULT_PROMPT_TOKENS
    completion_tokens: int = DEFAULT_COMPLETION_TOKENS
    cached_tokens: int = DEFAULT_CACHED_TOKENS

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
    model without an explicit script.

    Usage::

        mock = MockProvider(
            scripts={"echo": Script(kind="text", content="Hello!")},
            default=Script(kind="stream", content="Default reply"),
        )
        response = await mock.chat_completion(ChatCompletionRequest(...))
    """

    def __init__(
        self,
        scripts: Mapping[str, Script] | None = None,
        default: Script | None = None,
    ) -> None:
        self._scripts: dict[str, Script] = dict(scripts or {})
        self._default = default or Script(kind="text", content="Hello from mock provider")
        # Every request is recorded so tests can assert what was sent.
        self.calls: list[ChatCompletionRequest] = []

    def script(self, model: str, script: Script) -> None:
        """Register a script for a model name."""
        self._scripts[model] = script

    def _script_for(self, model: str) -> Script:
        return self._scripts.get(model, self._default)

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse | ProviderError:
        """Return a scripted non-streaming response."""
        self.calls.append(request)
        script = self._script_for(request.model)
        return self._render_nonstream(script, request.model)

    async def chat_completion_stream(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[StreamChunk | ProviderError]:
        """Yield a scripted streaming response."""
        self.calls.append(request)
        script = self._script_for(request.model)

        if script.kind in ("error", "rate_limit"):
            yield self._render_error(script)
            return

        async for chunk in self._render_stream(script, request.model):
            yield chunk

    # ── Rendering ────────────────────────────────────────────────────

    def _render_nonstream(
        self, script: Script, model: str
    ) -> ChatCompletionResponse | ProviderError:
        if script.kind == "error":
            return self._render_error(script)
        if script.kind == "rate_limit":
            return self._render_error(script)
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
            )
        return ProviderError(
            code=script.error_code or "server_error",
            message=script.content or "Mock provider error",
            status_code=script.status_code or 500,
            retryable=script.status_code in (429, 500, 502, 503, 504),
        )

    async def _render_stream(self, script: Script, model: str) -> AsyncIterator[StreamChunk]:
        chunk_id = f"mock-stream-{model}"

        if script.kind == "tool_call":
            # Emit role chunk, then arguments, then finish chunk with usage.
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
            yield StreamChunk(
                id=chunk_id,
                delta=Delta(),
                finish_reason="tool_calls",
                usage=script.usage,
            )
            return

        # Plain text: split content into word-sized deltas so consumers must
        # accumulate deltas rather than assume one chunk per response.
        words = script.content.split(" ") if script.content else []
        if words:
            yield StreamChunk(
                id=chunk_id,
                delta=Delta(content=" ".join(words[:1])),
                finish_reason=None,
            )
            for i in range(1, len(words)):
                yield StreamChunk(
                    id=chunk_id,
                    delta=Delta(content=f" {words[i]}" if i > 0 else words[i]),
                    finish_reason=None,
                )
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
