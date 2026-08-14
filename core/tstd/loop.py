"""Agent loop — runs inside ``SessionRunner``.

Ports the ``plan → gate → act → verify → reconcile`` structure from
tst-cua (see ``REUSE.md`` §2.1) onto the TST Desk 3-tier router and
async provider client.

The loop is provider-agnostic: it accepts any ``ProviderClient`` and
derives model slugs from the ``ModelConfig``.  No vendor-specific
assumptions appear in the control flow.

**Coverage** (TD-401 + TD-402): wait for user message → determine tier
→ call provider (streaming) → emit events → execute tool calls with
the dispatcher → feed results back to the model → record turn.
Tool calls are validated, dispatched (parallel where safe), and results
truncated.  The ``gate`` and ``verify`` phases of the tst-cua cycle are
added in E7 (autonomy hooks) and E8 (approvals).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from .compaction import maybe_compact
from .config import ModelConfig
from .context import PromptAssembler
from .context.stack import build_instruction_stack
from .context.tokens import TokenCounter, make_token_counter
from .cost import CostTracker
from .logging import get_logger
from .protocol import AssistantDelta, ContextCompacted, SteeringReloaded, TurnComplete
from .protocol import ToolCall as ToolCallEvent
from .protocol import ToolResult as ToolResultEvent
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
from .provider import (
    ToolDefinition as ProviderToolDefinition,
)
from .router import TierName, TierRouter
from .session import Session
from .tools import ToolDispatcher, ToolRegistry


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


log = get_logger("tstd.loop")


def _stream_turn(
    provider: ProviderLike,
    model: str,
    messages: list[ChatMessage],
    tools: list[ProviderToolDefinition] | None = None,
) -> AsyncIterator[StreamChunk | ProviderError]:
    """Open a stream from the provider.

    Copies the message list so that the provider never sees the caller's
    post-stream mutations (e.g. appending the assistant response).
    Returns an ``AsyncIterator`` ready for ``async for``.

    If *tools* is provided, it is passed as the ``tools`` parameter to
    the chat completion request.
    """
    request = ChatCompletionRequest(
        model=model,
        messages=list(messages),
        tools=tools,
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


def _parse_tool_call_stream(
    tool_calls: dict[int, dict[str, str | int]],
    chunk: StreamChunk,
) -> None:
    """Accumulate tool call deltas from a stream chunk into *tool_calls*."""
    if not chunk.delta.tool_calls:
        return
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
            args = entry["arguments"]
            assert isinstance(args, str)
            entry["arguments"] = args + tc.function_arguments


async def _stream_and_parse(
    provider: ProviderLike,
    model_slug: str,
    messages: list[ChatMessage],
    session: Session,
    tool_definitions: list[ProviderToolDefinition] | None,
    tracker: CostTracker,
    tier: TierName,
    tier_cfg: Any,
) -> tuple[str, dict[int, dict[str, str | int]], bool, str]:
    """Call the provider, stream deltas, and accumulate tool calls.

    Returns:
        A tuple of ``(collected_content, tool_calls, failed, error_msg)``.
    """
    collected_content = ""
    tool_calls: dict[int, dict[str, str | int]] = {}
    failed = False
    error_msg = ""

    async for chunk in _stream_turn(provider, model_slug, messages, tool_definitions):
        if session.cancel_requested:
            return collected_content, tool_calls, True, "cancelled"

        if isinstance(chunk, ProviderError):
            return collected_content, tool_calls, True, chunk.message

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
        _parse_tool_call_stream(tool_calls, chunk)

        if chunk.finish_reason and chunk.usage:
            tracker.record(tier, chunk.usage, tier_cfg)

    return collected_content, tool_calls, failed, error_msg


async def _build_assistant_tool_call(
    session: Session,
    tool_calls: dict[int, dict[str, str | int]],
) -> list[ProviderToolCall]:
    """Build provider-format tool calls from the accumulated deltas.

    Emits a ``ToolCall`` event for each tool call.

    Returns:
        A list of ``ProviderToolCall`` objects ready to append to the
        conversation.
    """
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

        # Emit a ToolCall event for the client
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

    return provider_tool_calls


async def _dispatch_and_append_results(
    dispatcher: ToolDispatcher,
    session: Session,
    messages: list[ChatMessage],
    tool_calls: dict[int, dict[str, str | int]],
) -> None:
    """Execute tool calls and append their results to the conversation.

    Emits ``ToolResult`` events for each dispatch result.
    """
    # Build (tool_call_id, name, arguments) list for batch dispatch
    dispatch_items: list[tuple[str, str, dict[str, Any]]] = []
    for idx in sorted(tool_calls, key=lambda k: k):
        tc_sorted = tool_calls[idx]
        tc_id = str(tc_sorted["id"])
        tc_name = str(tc_sorted["name"])
        tc_args_str = str(tc_sorted["arguments"])

        parsed_args: dict[str, Any] = {}
        if tc_args_str:
            try:
                parsed_args = json.loads(tc_args_str)
            except json.JSONDecodeError:
                parsed_args = {"_raw": tc_args_str}

        dispatch_items.append((tc_id, tc_name, parsed_args))

    # Skip dispatch if the session was cancelled during streaming
    if session.cancel_requested:
        return

    # Dispatch (parallel-safe tools run concurrently)
    results = await dispatcher.dispatch_many(dispatch_items, session)

    # Append results as tool role messages and emit events
    for r in results:
        await session.event_log.add(
            ToolResultEvent(
                session_id=session.id,
                tool_call_id=r.tool_call_id,
                status=r.status,
                output=r.output,
                truncated=r.truncated,
                seq=1,
            )
        )

        messages.append(
            ChatMessage(
                role="tool",
                content=r.output,
                tool_call_id=r.tool_call_id,
            )
        )


async def agent_loop(
    session: Session,
    router: TierRouter,
    provider_factory: Callable[[], Awaitable[ProviderLike]],
    config: ModelConfig,
    tool_registry: ToolRegistry | None = None,
    tool_dispatcher: ToolDispatcher | None = None,
    prompt_assembler: PromptAssembler | None = None,
) -> None:
    """Agent loop — runs inside ``SessionRunner``.

    Maintains conversation state, routes provider calls through the
    tier router, and streams events to the session event log.

    When a tool registry and dispatcher are provided, tool calls are
    executed automatically and their results are fed back to the model
    in a round-trip loop.  Without a dispatcher, tool calls are still
    parsed and emitted as events but not executed (TD-401 mode).

    Args:
        session: The session this loop belongs to.
        router: Tier routing policy (brain → worker → validator).
        provider_factory: Async callable that returns a ``ProviderLike``
            (network or mock).  Called once before the first turn so the
            session can be opened without a provider being available.
        config: Model configuration with tier pricing and slugs.
        tool_registry: Optional tool registry.  If provided, tool
            definitions are sent to the model.
        tool_dispatcher: Optional tool dispatcher.  If provided, tool
            calls are executed.  Requires *tool_registry*.
        prompt_assembler: Optional :class:`PromptAssembler` (TD-305).
            If ``None``, one is constructed from
            ``session.workspace_path``.  Injects the per-tier system
            prompt in stable-prefix order and logs the cache prefix
            hash for observability.
    """
    # ── Conversation state ──────────────────────────────────────────
    assembler = prompt_assembler or PromptAssembler(session.workspace_path)
    # Conversation messages only; the system message is assembled per
    # turn below (TD-305).
    messages: list[ChatMessage] = []
    tracker = CostTracker(config)
    provider: ProviderLike | None = None  # resolved lazily before first use

    # Pre-compute tool definitions if we have a registry
    tool_definitions: list[ProviderToolDefinition] | None = None
    if tool_registry is not None:
        tool_definitions = tool_registry.to_provider_definitions()

    # Tracks the last steering prefix hash for change detection (TD-509).
    _last_prefix_hash: str | None = None

    # One token counter per model slug (TD-405); encoders load once.
    _token_counters: dict[str, TokenCounter] = {}

    # ── Turn loop ───────────────────────────────────────────────────
    while not session.cancel_requested:
        # 1. Wait for user input
        user_content = await session.wait_for_user_message()
        if user_content is None:
            break  # session was cancelled

        messages.append(ChatMessage(role="user", content=user_content))

        # 2. Tool-call round-trip loop
        #    Each iteration: call provider → execute tool calls → loop
        #    until the model returns a text response.
        while True:
            # 2a. Determine active tier via router
            tier = router.record_turn_start()
            tier_cfg = config.tier(tier)
            tracker.begin_turn()
            turn_start = time.time()

            # 2b. Assemble the per-tier system prompt in stable-prefix
            #     order (TD-305) and place it before the conversation.
            assembled = await assembler.assemble(
                tier,
                task=user_content if tier == "worker" else None,
            )
            if messages and messages[0].role == "system":
                messages[0] = ChatMessage(role="system", content=assembled.text)
            else:
                messages.insert(0, ChatMessage(role="system", content=assembled.text))

            # 2b.1 Steering hot reload (TD-509): when the steering
            #     prefix hash differs from the last turn's, steering
            #     files changed.  Announce the reload in the timeline
            #     and push an updated instruction stack to the
            #     inspector.  Turn-boundary comparison debounces rapid
            #     successive saves — one event per changed state.
            if _last_prefix_hash is not None and assembled.prefix_hash != _last_prefix_hash:
                await session.event_log.add(
                    SteeringReloaded(
                        session_id=session.id,
                        prefix_hash=assembled.prefix_hash,
                        prefix_tokens=assembled.prefix_tokens,
                        source_count=len(assembled.steering.sources),
                        seq=1,  # overwritten by the event log
                    )
                )
                await session.event_log.add(
                    build_instruction_stack(session.id, assembled.steering, seq=1)
                )
                log.info(
                    "steering reloaded",
                    extra={
                        "extra_fields": {
                            "session_id": session.id,
                            "prefix_hash": assembled.prefix_hash,
                            "prefix_tokens": assembled.prefix_tokens,
                        }
                    },
                )
            _last_prefix_hash = assembled.prefix_hash

            # 2b.2 Context window guard (TD-405): when the estimated
            #     prompt approaches the tier's context window, compact
            #     older turns into a summary.  Cuts land at user-message
            #     boundaries, so in-flight tool-call pairs are never
            #     split.  Announced on the timeline, never silent.  The
            #     steering block and manifest were just re-read from
            #     disk at 2b, so instructions survive compaction.
            counter = _token_counters.get(tier_cfg.slug)
            if counter is None:
                counter = make_token_counter(tier_cfg.slug)
                _token_counters[tier_cfg.slug] = counter
            compacted, compaction = maybe_compact(messages, tier_cfg, counter)
            if compaction is not None:
                messages = compacted
                await session.event_log.add(
                    ContextCompacted(
                        session_id=session.id,
                        dropped_messages=compaction.dropped_messages,
                        kept_messages=compaction.kept_messages,
                        tokens_before=compaction.tokens_before,
                        tokens_after=compaction.tokens_after,
                        seq=1,  # overwritten by the event log
                    )
                )
                log.info(
                    "context compacted",
                    extra={
                        "extra_fields": {
                            "session_id": session.id,
                            "tier": tier,
                            "dropped_messages": compaction.dropped_messages,
                            "tokens_before": compaction.tokens_before,
                            "tokens_after": compaction.tokens_after,
                            "counter_method": compaction.counter_method,
                        }
                    },
                )

            log.info(
                "turn start",
                extra={
                    "extra_fields": {
                        "session_id": session.id,
                        "tier": tier,
                        "model": tier_cfg.slug,
                        "turn": router.turn_count,
                        "cache_prefix_hash": assembled.prefix_hash,
                        "cache_prefix_tokens": assembled.prefix_tokens,
                    }
                },
            )

            # 2c. Resolve provider lazily on first use
            if provider is None:
                provider = await provider_factory()

            # 2d. Call provider (streaming)
            collected_content, tool_calls, failed, error_msg = await _stream_and_parse(
                provider,
                tier_cfg.slug,
                messages,
                session,
                tool_definitions,
                tracker,
                tier,
                tier_cfg,
            )

            # 2e. Handle failure
            if failed:
                # If the session was cancelled during streaming, bail out
                # without emitting a turn_complete or recording a failure.
                if session.cancel_requested:
                    break

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
                break  # exit tool-call loop, wait for next user message

            # 2e. Record success
            router.record_success()

            # 2f. Append assistant response to conversation
            if tool_calls:
                provider_tool_calls = await _build_assistant_tool_call(session, tool_calls)
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=None,
                        tool_calls=provider_tool_calls,
                    )
                )

                # 2g. Execute tool calls via dispatcher (if available)
                if tool_dispatcher is not None:
                    # Yield control so the event loop can process cancellation
                    # between the ToolCall event emission and the dispatch.
                    # 0.05s is enough for the test to detect the event and cancel.
                    await asyncio.sleep(0.05)
                    await _dispatch_and_append_results(
                        tool_dispatcher, session, messages, tool_calls
                    )
                    # If cancelled during dispatch, exit the tool-call loop
                    if session.cancel_requested:
                        break
                    # Loop back to call the provider again with tool results
                    continue
                # Without a dispatcher, fall through to emit turn_complete
            else:
                messages.append(ChatMessage(role="assistant", content=collected_content or ""))

            # 2h. No tool calls (or no dispatcher) — turn is complete
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
                        "cache_prefix_hash": assembled.prefix_hash,
                        "cache_ratio": round(tracker.turn_cache_ratio(), 4),
                    }
                },
            )
            break
