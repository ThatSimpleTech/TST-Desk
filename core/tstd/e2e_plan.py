"""What one headless-harness pass runs against (TD-1401, TD-1803).

Split out so the harness and its live leg can both name these types without
importing each other: ``e2e_harness`` builds the mock plan, ``e2e_live``
builds the live one, and neither has to know the other exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol

from .policy import PolicyConfig
from .provider import ChatCompletionRequest, ChatCompletionResponse, ProviderError, StreamChunk


class RecordingProvider(Protocol):
    """A ``ProviderLike`` that keeps the requests it was sent.

    The harness asserts on the *first system prompt* to prove steering
    reached the model, and no wire event carries that.  ``MockProvider``
    already records; the live wrapper records to the same shape.
    """

    calls: list[ChatCompletionRequest]

    def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[StreamChunk | ProviderError]: ...

    async def chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse | ProviderError: ...


@dataclass(frozen=True)
class HarnessPlan:
    """Everything that differs between a mock pass and a live pass.

    Data, not subclasses: the harness has one code path, so a live
    regression cannot hide behind a branch the mock never takes.

    Attributes:
        provider: The provider the daemon runs against.
        steering: ``AGENTS.md`` content for the harness workspace.
        prompt: The single user message that drives the turn.
        approve_on: Event type that carries a pending approval —
            ``approval_request`` once a gate can open, ``tool_call`` for
            TD-1401's ungated script.
        content_ok: Whether the file the agent wrote holds what was asked.
        expect_spend: Whether the turn must bill.  False for a zero-price
            local preset, where free is the correct answer and the ledger
            check carries the "still tracked" guarantee instead.
        turn_timeout: How long to wait for ``turn_complete``.
        budget_secs: Wall-clock budget for the whole pass.
        policy: Workspace approval policy, or None for the defaults.
    """

    provider: RecordingProvider
    steering: str
    prompt: str
    approve_on: str
    content_ok: Callable[[str], bool]
    expect_spend: bool
    turn_timeout: float
    budget_secs: float
    policy: PolicyConfig | None = None
