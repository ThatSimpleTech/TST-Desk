"""Agent loop — runs inside ``SessionRunner``.

Ports the ``plan → gate → act → verify → reconcile`` structure from
tst-cua (see ``REUSE.md`` §2.1) onto the TST Desk 3-tier router and
async provider client.

The loop is provider-agnostic: it accepts any ``ProviderClient`` and
derives model slugs from the ``ModelConfig``.  No vendor-specific
assumptions appear in the control flow.

**Current coverage** (TD-401): wait for user message → determine tier
(calls ``TierRouter``) → call provider (streaming) → emit events →
record turn.  Tool calls are parsed and emitted as events but their
execution is deferred to TD-402 (tool dispatch).  The ``gate`` and
``verify`` phases of the tst-cua cycle are added in E7 (autonomy hooks)
and E8 (approvals).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from .config import ModelConfig
from .cost import CostTracker
from .logging import get_logger
from .protocol import AssistantDelta, TurnComplete
from .protocol import ToolCall as ToolCallEvent
from .provider import (
    ChatCompletionRequest,
    ChatMessage,
    FunctionCall,
    ProviderError,
    StreamChunk,
)
from .provider import (
    ToolCall as ProviderToolCall,
)
from .router import TierName, TierRouter
from .session import Session

log = get_logger("tstd.loop")


class ProviderLike(Protocol):
    """Structural protocol for the provider the loop talks to.

    Both ``ProviderClient`` (network) and ``MockProvider`` (offline)
    satisfy this protocol, so the loop is provider-agnostic.

    Note: ``chat_completion_stream`` is defined as a regular (non-async)
    method returning ``AsyncIterator`` because the implementations are
    async generators (``async def`` with ``yield``).  When called, they
    return an ``AsyncIterator`` directly, not a coroutine.
    """

    def chat_completion_stream(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[StreamChunk | ProviderError]: ...


# Placeholder system prompt — replaced by full steering assembly in
# TD-305 (cache-aware prompt assembly) and TD-501 (file discovery).
_PLACEHOLDER_SYSTEM_PROMPT = (
    "You are a helpful assistant with access to filesystem and shell tools. "
    "Respond to the user's request and call tools when appropriate."
)


def _stream_turn(
    provider: ProviderLike,
    model: str,
    messages: list[ChatMessage],
) -> AsyncIterator[StreamChunk | ProviderError]:
    """Open a stream from the provider.

    Copies the message list so that the provider never sees the caller's
    post-stream mutations (e.g. appending the assistant response).
    Returns an ``AsyncIterator`` ready for ``async for``.
    """
    request = ChatCompletionRequest(
        model=model,
        messages=list(messages),
        stream=True,
    )
    return provider.chat_completion_stream(request)


async def _emit_turn_complete(
    session: Session,
    tier: TierName,
    turn_start: float,
    tracker: CostTracker,
    failed: bool = False,
) -> None:
    """Emit a ``turn_complete`` event with cost and duration."""
    duration = time.time() - turn_start
    await session.event_log.add(
        TurnComplete(
            session_id=session.id,
            tokens=tracker.turn_tokens(),
            cost=tracker.turn_cost(),
            tier=tier,
            duration=round(duration, 3),
            seq=1,  # overwritten by event log
        )
    )


async def agent_loop(
    session: Session,
    router: TierRouter,
    provider_factory: Callable[[], Awaitable[ProviderLike]],
    config: ModelConfig,
) -> None:
    """Agent loop — runs inside ``SessionRunner``.

    Maintains conversation state, routes provider calls through the
    tier router, and streams events to the session event log.

    Args:
        session: The session this loop belongs to.
        router: Tier routing policy (brain → worker → validator).
        provider_factory: Async callable that returns a ``ProviderLike``
            (network or mock).  Called once before the first turn so the
            session can be opened without a provider being available.
        config: Model configuration with tier pricing and slugs.
    """
    # ── Conversation state ──────────────────────────────────────────
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=_PLACEHOLDER_SYSTEM_PROMPT),
    ]
    tracker = CostTracker(config)
    provider: ProviderLike | None = None  # resolved lazily before first use

    # ── Turn loop ───────────────────────────────────────────────────
    while not session.cancel_requested:
        # 1. Wait for user input
        user_content = await session.wait_for_user_message()
        if user_content is None:
            break  # session was cancelled

        messages.append(ChatMessage(role="user", content=user_content))

        # 2. Determine active tier via router
        tier = router.record_turn_start()
        tier_cfg = config.tier(tier)
        tracker.begin_turn()
        turn_start = time.time()

        log.info(
            "turn start",
            extra={
                "extra_fields": {
                    "session_id": session.id,
                    "tier": tier,
                    "model": tier_cfg.slug,
                    "turn": router.turn_count,
                }
            },
        )

        # 3. Resolve provider lazily on first use
        if provider is None:
            provider = await provider_factory()

        # 4. Call provider (streaming)
        collected_content = ""
        tool_calls: dict[int, dict[str, str | int]] = {}
        failed = False
        error_msg = ""

        async for chunk in _stream_turn(provider, tier_cfg.slug, messages):
            if isinstance(chunk, ProviderError):
                failed = True
                error_msg = chunk.message
                break

            # Stream content delta
            if chunk.delta.content:
                collected_content += chunk.delta.content
                await session.event_log.add(
                    AssistantDelta(
                        session_id=session.id,
                        delta=chunk.delta.content,
                        seq=1,
                    )
                )

            # Accumulate tool call deltas
            if chunk.delta.tool_calls:
                for tc in chunk.delta.tool_calls:
                    entry = tool_calls.get(tc.index)
                    if entry is None:
                        entry = {
                            "index": tc.index,
                            "id": tc.id or "",
                            "name": tc.function_name or "",
                            "arguments": "",
                        }
                        tool_calls[tc.index] = entry
                    if tc.function_name:
                        entry["name"] = tc.function_name
                    if tc.function_arguments:
                        # mypy: "arguments" is str
                        args = entry["arguments"]
                        assert isinstance(args, str)
                        entry["arguments"] = args + tc.function_arguments

            if chunk.finish_reason and chunk.usage:
                tracker.record(tier, chunk.usage, tier_cfg)

        # 5. Handle failure
        if failed:
            router.record_failure()
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=f"I encountered an error: {error_msg}",
                )
            )
            await _emit_turn_complete(session, tier, turn_start, tracker, failed=True)
            log.warning(
                "turn failed",
                extra={
                    "extra_fields": {
                        "session_id": session.id,
                        "tier": tier,
                        "turn": router.turn_count,
                        "error": error_msg[:200],
                    }
                },
            )
            continue

        # 6. Record success
        router.record_success()

        # 7. Append assistant response to conversation
        if tool_calls:
            # Build ToolCall list for the protocol event and conversation
            provider_tool_calls: list[ProviderToolCall] = []
            for idx in sorted(tool_calls, key=lambda k: k):
                tc_sorted = tool_calls[idx]
                tc_id = str(tc_sorted["id"])
                tc_name = str(tc_sorted["name"])
                tc_args = str(tc_sorted["arguments"])

                provider_tool_calls.append(
                    ProviderToolCall(
                        id=tc_id,
                        type="function",
                        function=FunctionCall(
                            name=tc_name,
                            arguments=tc_args,
                        ),
                    )
                )

                # Emit a ToolCall event for the client (decision_class
                # is set by the classifier in E7 — for now it is None)
                parsed_args: dict[str, object] = {}
                if tc_args:
                    try:
                        parsed_args = json.loads(tc_args)
                    except json.JSONDecodeError:
                        parsed_args = {"_raw": tc_args}
                await session.event_log.add(
                    ToolCallEvent(
                        session_id=session.id,
                        tool_call_id=tc_id,
                        name=tc_name,
                        arguments=parsed_args,
                        decision_class=None,
                        seq=1,
                    )
                )

            messages.append(
                ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=provider_tool_calls,
                )
            )
        else:
            messages.append(ChatMessage(role="assistant", content=collected_content or ""))

        # 8. Emit turn_complete
        await _emit_turn_complete(session, tier, turn_start, tracker)

        log.info(
            "turn complete",
            extra={
                "extra_fields": {
                    "session_id": session.id,
                    "tier": tier,
                    "turn": router.turn_count,
                    "tokens": tracker.turn_tokens(),
                    "cost": tracker.turn_cost(),
                    "has_tool_calls": bool(tool_calls),
                }
            },
        )
