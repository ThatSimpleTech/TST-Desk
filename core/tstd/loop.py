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
import base64
import functools
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from .audit_writer import ModelCallSink, forward_classifier_judgment
from .autonomy import (
    AmbiguousClassifier,
    Boundary,
    Checkpointer,
    DecisionClass,
    DecisionClassifier,
    DecisionLedger,
    WorkerChatJudgmentBackend,
)
from .autonomy.breakers import record_autonomy_round
from .autonomy.checkpoint import auto_branch
from .autonomy.connector import classifier_connector, judgment_backend_for
from .autonomy.dod import make_dod_poller
from .autonomy.judgment import JudgmentBackend, note_judgment_completion_cost
from .autonomy.runner import advance_autonomy
from .autonomy.supervisor import attach_drift_check
from .autonomy.verify import (
    clear_turn_writes,
    maybe_verify_after_turn,
    note_tool_result,
)
from .autonomy.wakeup import deliver_wakeup
from .autonomy.worker import JudgmentAudit
from .compaction import ContextMeter, budget_threshold, estimate_tokens
from .compaction_files import FileAccess
from .compaction_model import compact_auto
from .compaction_wire import attach_summarizer
from .config import (
    ClearToolResultsConfig,
    ConfigError,
    JudgmentsConfig,
    ModelConfig,
    ModelDiscoveryError,
    TierConfig,
    resolve_base_url,
    resolve_credential_id,
)
from .context import PromptAssembler
from .context.calibration import (
    CalibratedTokenCounter,
    CalibrationStore,
    calibration_key,
    effective_chars_per_token,
    process_calibration_store,
    tier_chars_per_token,
)
from .context.embeddings import EmbeddingsClient, load_memory_for_turn
from .context.manifest import ManifestConfig, manifest_config_for_window
from .context.skills import apply_slash_skill, list_workspace_skills
from .context.stack import build_instruction_stack
from .context.tokens import TokenCounter, make_token_counter
from .context_clear import clear_stale_tool_results
from .context_fit import fit_inflight_turn, tool_result_char_cap, transcript_failure_text
from .cost import CallRecord, CostTracker
from .cu_verify import ActuationVerifier
from .discovery import discover_model, resolve_tier_slugs
from .fallback import BackupChain, TurnBackups, announce_switch, backup_wanted
from .keychain import KeychainError
from .local_worker import (
    effective_tier,
    mark_cu_tool,
    session_is_cu_heavy,
    titlebar_hosts,
    titlebar_slugs,
)
from .logging import get_logger, redact_secrets
from .mcp.deferral import McpToolDeferral
from .memory_commit import MemoryCommitter
from .output_cap import (
    LENGTH_FINISH,
    arguments_complete,
    cut_call_reply,
    log_output_cap,
    stub_call,
)
from .plan_mode import with_plan_note
from .plan_review import carry_for
from .policy import read_guards, save_approved_imports, with_run_rules
from .progress_guard import (
    PROGRESS_CHECK_INSTRUCTION,
    ProgressGuard,
    interpret_check,
    pause_reason_for,
    tool_note,
)
from .prompt_images import cap_prompt_images
from .protocol import (
    AssistantDelta,
    AssistantReasoning,
    ContextCompacted,
    DaemonEvent,
    DecisionLogged,
    RuleActivated,
    SteeringReloaded,
    TierState,
    TurnComplete,
    UserTurn,
)
from .protocol import CheckpointNotice as CheckpointNoticeEvent
from .protocol import ToolCall as ToolCallEvent
from .protocol import ToolResult as ToolResultEvent
from .provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    FunctionCall,
    ProviderError,
    StreamChunk,
    Usage,
    content_as_text,
    flatten_reasoning_details,
    merge_reasoning_details,
)
from .provider import (
    ToolCall as ProviderToolCall,
)
from .provider import (
    ToolDefinition as ProviderToolDefinition,
)
from .reply_retry import StreamResult, stream_reply
from .router import TierName, TierRouter
from .session import Session, SessionEventLog
from .session_goal import (
    answer_goal_command,
    goal_provider_content,
    parse_goal_command,
    set_goal,
)
from .shell_read_only import probe_git
from .steer import inject_steers
from .stop_check import TurnStopCheck
from .tools import ToolDispatcher, ToolRegistry
from .tools.boundary import PathGuard
from .tools.consult import offered_tools
from .tools.dispatch import build_decision_request, canonicalize_path_fields
from .tools.tool_search import TOOL_SEARCH_NAME
from .workspace_trust import trusted_imports
from .worktree_config import config_home, refresh_config


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

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse | ProviderError:
        """Send a single non-streaming completion request."""

        ...


log = get_logger("tstd.loop")


async def _invoke_factory(
    factory: Callable[..., Awaitable[ProviderLike]],
    tier_cfg: TierConfig,
) -> ProviderLike:
    """Call *factory* with the effective tier; zero-arg factories still work."""
    try:
        return await factory(tier_cfg)
    except TypeError:
        return await factory()


def _to_literal(cls: DecisionClass | None) -> Literal["A", "B", "C"] | None:
    """Convert a decision class enum to the wire-format literal string."""
    if cls is None:
        return None
    return cls.value


_GUARD_REASON_LIMIT = 400

# The worker's one-word verdict calls (classifier, DoD polls, judgment
# calls) cap their own reply; the tier's max_output_tokens is for turns.
_VERDICT_MAX_TOKENS = 8

#: (base_url, model) already warned about an empty verdict at the cap
#: (TD-5012). Verdicts run on every ambiguous tool call and every DoD
#: poll, so one warning per endpoint per process is enough.
_VERDICT_CAP_WARNED: set[tuple[str, str]] = set()


def _warn_verdict_cap_once(tier_cfg: TierConfig, base_url: str, model: str) -> None:
    """Say once why a verdict came back empty at its cap (TD-5012).

    Empty content with ``finish_reason: "length"`` on an 8-token call
    means the model spent the cap before it answered, almost always
    thinking. The parse then fails closed (Class B, or no DoD verdict),
    which looks like a judgment rather than a config gap.

    *base_url* is the host the call went to (``resolve_base_url``), so a
    bound credential's host wins over the tier URL (TD-1718). With
    ``verdict_extra_body`` already set the advice changes: the server
    seems to ignore the switch, so its spelling is the suspect.
    """
    key = (base_url, model)
    if key in _VERDICT_CAP_WARNED:
        return
    _VERDICT_CAP_WARNED.add(key)
    if tier_cfg.verdict_extra_body is None:
        advice = "Set verdict_extra_body on the worker tier to turn thinking off for verdict calls"
    else:
        advice = (
            "verdict_extra_body is set on the worker tier but the server seems to ignore "
            "it; check its switch is spelled the way the server expects"
        )
    log.warning(
        "worker verdict call hit its token cap with no answer; the model probably "
        "spent the cap thinking. " + advice,
        extra={
            "extra_fields": {
                "tier": "worker",
                "base_url": base_url,
                "model": model,
                "max_tokens": _VERDICT_MAX_TOKENS,
                "verdict_extra_body_set": tier_cfg.verdict_extra_body is not None,
            }
        },
    )


async def _clear_stale_results(
    session: Session,
    messages: list[ChatMessage],
    tier: TierName,
    tier_cfg: TierConfig,
    counter: TokenCounter,
    tool_definitions: list[ProviderToolDefinition] | None,
    policy: ClearToolResultsConfig,
) -> None:
    """Clear earlier turns' tool results in place when over budget (TD-5115).

    Announced like the in-turn trim: ``context_compacted`` with its own
    trigger, so the window shows a quiet row rather than a compaction.
    """
    cleared, stats = clear_stale_tool_results(messages, tier_cfg, counter, policy, tool_definitions)
    if stats is None:
        return
    messages[:] = cleared
    await session.conversation_changed()
    await session.event_log.add(
        ContextCompacted(
            session_id=session.id,
            dropped_messages=stats.cleared_results,
            kept_messages=stats.kept_messages,
            tokens_before=stats.tokens_before,
            tokens_after=stats.tokens_after,
            trigger="clear_stale",
            seq=1,  # overwritten by the event log
        )
    )
    log.info(
        "stale tool results cleared",
        extra={
            "extra_fields": {
                "session_id": session.id,
                "tier": tier,
                "cleared_results": stats.cleared_results,
                "tokens_before": stats.tokens_before,
                "tokens_after": stats.tokens_after,
                "counter_method": stats.counter_method,
            }
        },
    )


async def _show_progress_note(session: Session, messages: list[ChatMessage], text: str) -> None:
    """Put one short assistant line on the transcript and in the chat."""
    messages.append(ChatMessage(role="assistant", content=text))
    await session.conversation_changed()
    await session.event_log.add(AssistantDelta(session_id=session.id, delta=text, seq=1))


async def _finish_progress_answer(
    session: Session,
    tracker: CostTracker,
    tier: TierName,
    turn_start: float,
    config: ModelConfig,
    client_for: Callable[..., Awaitable[ProviderLike]],
    assembler: PromptAssembler,
    router: TierRouter,
    calibration: CalibrationStore,
    *,
    check_failed: bool = False,
) -> None:
    """The progress check answered the user. End the turn on that answer.

    *check_failed* ends it as a failed done-check (TD-5124).
    """
    router.record_success()
    await _emit_turn_complete(
        session,
        tier,
        turn_start,
        tracker,
        failed=check_failed,
        error_code="stop_check_failed" if check_failed else None,
    )
    await maybe_verify_after_turn(session, config, tracker, client_for, assembler, calibration)
    session.snapshot_branches()
    log.info(
        "turn complete",
        extra={"extra_fields": {"session_id": session.id, "progress_guard": "done"}},
    )


async def _progress_check_gate(
    guard: ProgressGuard,
    session: Session,
    messages: list[ChatMessage],
    provider: ProviderLike,
    model_slug: str,
    tracker: CostTracker,
    tier: TierName,
    tier_cfg: Any,
    turn_start: float,
    config: ModelConfig,
    client_for: Callable[..., Awaitable[ProviderLike]],
    assembler: PromptAssembler,
    router: TierRouter,
    calibration: CalibrationStore,
    stop_check: TurnStopCheck | None = None,
) -> Literal["proceed", "pause", "stop"]:
    """Ask the tool-free question. ``stop`` means the turn is over.

    A DONE answer ends the turn, so the done-check runs on it (TD-5124):
    a failure sends the model back to work instead.
    """
    check_messages = [
        *messages,
        ChatMessage(role="user", content=PROGRESS_CHECK_INSTRUCTION),
    ]
    check = await _stream_and_parse(
        provider,
        model_slug,
        check_messages,
        session,
        None,
        tracker,
        tier,
        tier_cfg,
        emit_events=False,
    )
    content, tool_calls, failed, error_msg = (
        check.content,
        check.tool_calls,
        check.failed,
        check.error_msg,
    )
    if session.cancel_requested or error_msg == "cancelled":
        return "stop"
    if session.stop_turn_requested or error_msg == "stopped":
        await _finish_stopped_turn(session, tier, turn_start, tracker)
        return "stop"
    if failed:
        guard.pause_reason = pause_reason_for(error_msg or "the progress check failed")
        return "pause"
    if tool_calls and not (content or "").strip():
        guard.pause_reason = pause_reason_for("the progress check tried to call a tool")
        return "pause"
    kind, detail = interpret_check(content or "", guard.last_continue)
    if kind == "done":
        await _show_progress_note(session, messages, detail)
        checked = "end" if stop_check is None else await stop_check.at_turn_end(messages)
        if checked == "retry":
            guard.note_reply()
            return "proceed"
        if checked == "stopped":
            await _finish_stopped_turn(session, tier, turn_start, tracker)
            return "stop"
        await _finish_progress_answer(
            session,
            tracker,
            tier,
            turn_start,
            config,
            client_for,
            assembler,
            router,
            calibration,
            check_failed=checked == "failed",
        )
        return "stop"
    if kind == "continue":
        guard.note_auto_continue(detail)
        await _show_progress_note(session, messages, f"Next: {detail}")
        return "proceed"
    guard.pause_reason = pause_reason_for(detail)
    return "pause"


async def _interactive_guard(
    guard: ProgressGuard,
    session: Session,
    messages: list[ChatMessage],
    provider: ProviderLike,
    model_slug: str,
    tracker: CostTracker,
    tier: TierName,
    tier_cfg: Any,
    turn_start: float,
    config: ModelConfig,
    client_for: Callable[..., Awaitable[ProviderLike]],
    assembler: PromptAssembler,
    router: TierRouter,
    calibration: CalibrationStore,
    stop_check: TurnStopCheck | None = None,
) -> bool:
    """True when the loop should call the model. False ends this turn.

    Autonomy sessions keep their own breakers. A delegate child is not
    a session the user can resume, so it skips the guard too.
    """
    if session.autonomy or session.delegate_depth >= 1:
        return True
    gate = guard.before_model_call()
    if gate == "check":
        checked = await _progress_check_gate(
            guard,
            session,
            messages,
            provider,
            model_slug,
            tracker,
            tier,
            tier_cfg,
            turn_start,
            config,
            client_for,
            assembler,
            router,
            calibration,
            stop_check,
        )
        if checked == "stop":
            return False
        gate = checked
    if gate == "pause":
        log.warning(
            "progress guard pause",
            extra={"extra_fields": {"session_id": session.id, "summary": guard.pause_reason}},
        )
        await session.pause_at_cap(guard.pause_reason[:_GUARD_REASON_LIMIT])
        await session.wait_for_resume()
        if session.cancel_requested:
            return False
        if session.stop_turn_requested:
            await _finish_stopped_turn(session, tier, turn_start, tracker)
            return False
        guard.on_resume()
    guard.note_model_call()
    return True


def _cap_violation(
    session: Session,
    tracker: CostTracker,
    session_start: float,
    iterations: int,
    skip_all: bool = False,
) -> str | None:
    """The first declared cap that is exceeded, or ``None``.

    Returns a human-readable fault summary (TD-707) — checked before
    every model call so an over-cap session pauses instead of spending.
    Skip-all (TD-806) is skip-everything: caps do not pause.
    """
    # Skip-all is an interactive escape (TD-806). An unattended run
    # still stops on the signed caps (spec §12.7).
    if skip_all and not session.autonomy:
        return None
    caps = session.boundary_config.caps
    if tracker.session_cost() >= caps.spend_usd:
        return f"spend cap exceeded: ${tracker.session_cost():.4f} >= ${caps.spend_usd:.2f}"
    elapsed_hours = (time.time() - session_start) / 3600.0
    if elapsed_hours >= caps.wall_clock_hours:
        return f"wall-clock cap exceeded: {elapsed_hours:.2f}h >= {caps.wall_clock_hours}h"
    if iterations >= caps.max_iterations:
        return f"iteration cap exceeded: {iterations} >= {caps.max_iterations}"
    return None


def _stream_turn(
    provider: ProviderLike,
    model: str,
    messages: list[ChatMessage],
    tools: list[ProviderToolDefinition] | None = None,
    *,
    max_tokens: int | None = None,
    extra_body: dict[str, Any] | None = None,
) -> AsyncIterator[StreamChunk | ProviderError]:
    """Open a stream from the provider.

    Copies the message list so that the provider never sees the caller's
    post-stream mutations (e.g. appending the assistant response).
    Returns an ``AsyncIterator`` ready for ``async for``.

    If *tools* is provided, it is passed as the ``tools`` parameter to
    the chat completion request. *max_tokens* and *extra_body* come
    from the tier (TD-5008).
    """
    request = ChatCompletionRequest(
        model=model,
        messages=list(messages),
        tools=tools,
        stream=True,
        max_tokens=max_tokens,
        extra_body=extra_body,
    )
    return provider.chat_completion_stream(request)


async def _emit_turn_complete(
    session: Session,
    tier: TierName,
    turn_start: float,
    tracker: CostTracker,
    failed: bool = False,
    error_code: str | None = None,
) -> None:
    """Emit a ``turn_complete`` event with cost and duration.

    ``failed``/``error_code`` make a failed turn machine-visible so the UI
    can key tailored copy off the provider's typed code (TD-1008).
    """
    duration = time.time() - turn_start
    await session.event_log.add(
        TurnComplete(
            session_id=session.id,
            tokens=tracker.turn_tokens(),
            cost=tracker.turn_cost(),
            tier=tier,
            duration=round(duration, 3),
            failed=failed,
            error_code=error_code,
            seq=1,  # overwritten by event log
        )
    )
    await session.close_cu_session()


async def _finish_stopped_turn(
    session: Session,
    tier: TierName,
    turn_start: float,
    tracker: CostTracker,
) -> None:
    """Seal a turn the user stopped. The session stays open (TD-5003).

    ``failed`` stays false and the code is ``stopped``, which the UI
    treats as silence. The flag is cleared before the emit so a second
    check during the await cannot seal the turn twice.
    """
    if not session.stop_turn_requested:
        return
    if session.state == "paused":
        await session.set_state("running", reason="turn stopped")
    session.acknowledge_stop_turn()
    await _emit_turn_complete(
        session,
        tier,
        turn_start,
        tracker,
        failed=False,
        error_code="stopped",
    )
    log.info(
        "turn stopped",
        extra={"extra_fields": {"session_id": session.id, "tier": tier}},
    )


def _rule_rel_path(session: Session, path: Path) -> str:
    """Render a rule's source path workspace-relative for the timeline."""
    try:
        return path.relative_to(Path(session.workspace_path)).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(Path(session.workspace_path).resolve()).as_posix()
        except ValueError:
            return path.name


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


async def _close_stream(stream: AsyncIterator[StreamChunk | ProviderError]) -> None:
    """Close *stream* now, if it can be closed (an async generator can)."""
    close = getattr(stream, "aclose", None)
    if close is not None:
        await close()


async def _stream_and_parse(
    provider: ProviderLike,
    model_slug: str,
    messages: list[ChatMessage],
    session: Session,
    tool_definitions: list[ProviderToolDefinition] | None,
    tracker: CostTracker,
    tier: TierName,
    tier_cfg: TierConfig,
    *,
    emit_events: bool = True,
    on_usage: Callable[[Usage], None] | None = None,
    response_id: str | None = None,
) -> StreamResult:
    """Call the provider, stream deltas, and accumulate tool calls.

    The request caps generation at the tier's ``max_output_tokens`` and
    carries its ``extra_body`` (TD-5008), so the turn and the progress
    check both send them.

    *on_usage* receives the call's reported usage once the stream closes
    (TD-5007: the main turn call feeds token calibration from it).

    *response_id* stamps every delta and reasoning chunk this call
    emits, so a stall can withdraw exactly this response (TD-5159).

    Returns:
        A :class:`StreamResult` of ``(collected_content, tool_calls, failed,
        error_msg, error_code, raw_detail, finish_reason, reasoning_details,
        began, retryable, response_id, endpoint_failed)``.
        ``error_code`` is the provider's typed code (e.g. ``auth_failed``)
        so clients can key tailored copy off it (TD-1008). ``raw_detail`` is
        the upstream body for the log; it is empty unless the chunk is a
        ``ProviderError``. ``finish_reason`` is the last one the stream
        reported, or ``None``; ``"length"`` means the reply stopped at
        ``max_output_tokens`` (TD-5008). ``reasoning_details`` is the
        OpenRouter array to echo on the next assistant message (TD-1903),
        or ``None`` when the stream carried none. ``began`` says a chunk
        arrived, after which the provider no longer retries (TD-5159).
        ``retryable`` is the provider's flag on the error that ended the
        call, which is what moves a turn to a backup endpoint (TD-5120);
        ``endpoint_failed`` does the same for a reply the endpoint cut off
        partway, which is not retried.
    """
    collected_content = ""
    began = False
    tool_calls: dict[int, dict[str, str | int]] = {}
    collected_details: list[dict[str, Any]] = []
    failed = False
    retryable = False
    endpoint_failed = False
    error_msg = ""
    error_code: str | None = None
    raw_detail = ""
    finish_reason: str | None = None

    # TD-1804: usage is collected from any chunk that carries it and
    # recorded once, after the stream closes.  Providers disagree about
    # where it rides: Ollama sends it on a chunk *after* the one carrying
    # ``finish_reason``, OpenAI and vLLM on a trailing usage-only chunk, and
    # a co-emitting provider (``MockProvider``) puts both on one chunk.
    # Gating on both fields therefore dropped the row entirely for the first
    # two, and recording per usage-bearing chunk would bill a provider that
    # repeats cumulative usage more than once.  Reconciling at stream end is
    # one row and one cost_update per call for all of them; last-wins keeps
    # the complete cumulative figure rather than a partial first one.
    usage: Usage | None = None

    stream = _stream_turn(
        provider,
        model_slug,
        messages,
        tool_definitions,
        max_tokens=tier_cfg.max_output_tokens,
        extra_body=tier_cfg.extra_body,
    )
    try:
        async for chunk in stream:
            if session.cancel_requested or session.stop_turn_requested:
                failed = True
                error_msg = "cancelled" if session.cancel_requested else "stopped"
                break

            if isinstance(chunk, ProviderError):
                failed, error_msg, error_code = True, chunk.message, chunk.code
                raw_detail = chunk.detail
                retryable, endpoint_failed = chunk.retryable, chunk.endpoint_failed
                break
            began = True

            # Stream reasoning delta (TD-1901).  The plaintext is emitted,
            # never folded into `collected_content`: that string is replayed to
            # the provider as assistant speech, and feeding a model its own
            # scratchpad back as something it said is both wrong and paid for.
            # A reasoning model spends minutes here, so this is also the only
            # sign of life the window gets before the answer starts.
            # `reasoning_details` is different: OpenRouter requires the raw
            # array back on the next assistant message, so it is kept apart
            # (TD-1903).
            if chunk.delta.reasoning_details:
                merge_reasoning_details(collected_details, chunk.delta.reasoning_details)
            thinking = chunk.delta.reasoning
            if thinking is None and chunk.delta.reasoning_details:
                thinking = flatten_reasoning_details(chunk.delta.reasoning_details)
            if thinking and emit_events:
                await session.event_log.add(
                    AssistantReasoning(
                        session_id=session.id,
                        delta=thinking,
                        response_id=response_id,
                        seq=1,
                    )
                )

            # Stream content delta. A progress check turns emission off so a
            # BLOCKED line does not land in the chat as if it were the answer.
            if chunk.delta.content:
                collected_content += chunk.delta.content
                if emit_events:
                    await session.event_log.add(
                        AssistantDelta(
                            session_id=session.id,
                            delta=chunk.delta.content,
                            response_id=response_id,
                            seq=1,
                        )
                    )

            # Accumulate tool call deltas
            _parse_tool_call_stream(tool_calls, chunk)

            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason
    finally:
        # A generator cannot close itself when its consumer stops reading
        # (Stop, a provider error): left to the async-generator finalizer,
        # the connection stayed open until this function returned and a
        # task was scheduled to close it, and the server kept generating
        # meanwhile. Closing here hangs up before the turn moves on (TD-5158).
        await _close_stream(stream)

    # Tokens the provider reported are recorded even when the call ended
    # badly — they were spent either way, and a cancelled or failed call
    # that bills nothing is the one way an audit ledger can understate real
    # consumption.
    if usage is not None:
        tracker.record(tier, usage, tier_cfg)
        # TD-1006: the cost meter updates as costs accrue — one
        # cost_update per recorded call, not one per turn.
        await session.event_log.add(tracker.emit_cost_update(session.id))
        if on_usage is not None:
            on_usage(usage)

    return StreamResult(
        collected_content,
        tool_calls,
        failed,
        error_msg,
        error_code,
        raw_detail,
        finish_reason,
        collected_details or None,
        began,
        retryable,
        response_id,
        endpoint_failed,
    )


async def _stream_turn_reply(
    provider: ProviderLike,
    model_slug: str,
    messages: list[ChatMessage],
    session: Session,
    tool_definitions: list[ProviderToolDefinition] | None,
    tracker: CostTracker,
    tier: TierName,
    tier_cfg: TierConfig,
    counter: CalibratedTokenCounter,
    config: ModelConfig,
) -> StreamResult:
    """The turn's own model call. Only it teaches the token ratio (TD-5007).

    A stall after output began is withdrawn from the window and the call
    is made once more under ``provider_retry`` (TD-5159, reply_retry.py).
    """

    def attempt(response_id: str) -> Awaitable[StreamResult]:
        return _stream_and_parse(
            provider,
            model_slug,
            messages,
            session,
            tool_definitions,
            tracker,
            tier,
            tier_cfg,
            on_usage=_calibration_observer(counter, messages, tool_definitions),
            response_id=response_id,
        )

    return await stream_reply(attempt, session, config.provider_retry)


@dataclass
class _BackupReply:
    """Where a turn's call ended up after its tier's backups (TD-5120)."""

    reply: StreamResult
    tier_cfg: TierConfig
    counter: CalibratedTokenCounter
    provider: ProviderLike


def _keychain_failure(error: KeychainError) -> StreamResult:
    """A backup whose key is not stored fails the turn saying so (TD-1008)."""
    return StreamResult(
        content="",
        tool_calls={},
        failed=True,
        error_msg=str(error),
        error_code="missing_api_key",
        raw_detail="",
        finish_reason=None,
        reasoning_details=None,
        began=False,
    )


async def _reply_with_backups(
    reply: StreamResult,
    chain: BackupChain,
    *,
    session: Session,
    provider: ProviderLike,
    counter: CalibratedTokenCounter,
    messages: list[ChatMessage],
    tool_definitions: list[ProviderToolDefinition] | None,
    tracker: CostTracker,
    config: ModelConfig,
    client_for: Callable[[TierConfig], Awaitable[ProviderLike]],
    counter_for: Callable[[TierConfig], CalibratedTokenCounter],
    overflow_retried: bool,
    on_switch: Callable[[TierConfig], Awaitable[None]],
) -> _BackupReply:
    """Send the turn's request down the tier's backups until one answers.

    *reply* is the failed call to ``chain.current``, after its retry
    budget. Each backup gets the same conversation, fitted to its own
    window with its own calibrated counter, through its own client, and
    its own ``provider_retry`` budget (TD-5120). The chain stays where it
    stopped, so the rest of the turn uses that backup. When the last one
    fails too, its failure is the turn's.
    """
    tier_cfg = chain.current
    while backup_wanted(reply) and not (session.cancel_requested or session.stop_turn_requested):
        previous = tier_cfg
        backup = chain.advance()
        if backup is None:
            break
        tier_cfg = backup
        await announce_switch(session, chain, previous, reply)
        await on_switch(tier_cfg)
        counter = counter_for(tier_cfg)
        session.context_meter = ContextMeter(
            chain.tier, tier_cfg, counter, tool_definitions, fallback=chain.position
        )
        try:
            provider = await client_for(tier_cfg)
        except KeychainError as e:
            reply = _keychain_failure(e)
            break
        await _fit_prompt(
            session,
            messages,
            chain.tier,
            tier_cfg,
            counter,
            tool_definitions,
            overflow_retried=overflow_retried,
            max_prompt_images=config.computer_use.max_prompt_images,
            clear_policy=config.clear_tool_results,
        )
        reply = await _stream_turn_reply(
            provider,
            tier_cfg.require_slug(),
            messages,
            session,
            tool_definitions,
            tracker,
            chain.tier,
            tier_cfg,
            counter,
            config,
        )
    return _BackupReply(reply, tier_cfg, counter, provider)


def _carries_non_text(messages: list[ChatMessage]) -> bool:
    """True when any message has a part the estimate cannot size.

    That is an image part, or echoed ``reasoning_details`` (TD-1903):
    the estimate counts the array's JSON, but the provider bills the
    thinking it stands for, and an encrypted blob's length says nothing
    about that.
    """
    return any(
        bool(message.reasoning_details)
        or (
            isinstance(message.content, list)
            and any(part.get("type") != "text" for part in message.content)
        )
        for message in messages
    )


def _calibration_observer(
    counter: CalibratedTokenCounter,
    messages: list[ChatMessage],
    tools: list[ProviderToolDefinition] | None,
) -> Callable[[Usage], None] | None:
    """Usage callback that teaches *counter* this call's real size.

    The raw estimate is taken now, from the base counter, over exactly
    what is about to be sent. A calibrated estimate here would compound
    the ratio call after call. A screenshot is billed in tokens the text
    estimate never counts, so an image-bearing call teaches nothing.
    """
    if _carries_non_text(messages):
        return None
    raw_estimate, _ = estimate_tokens(messages, counter.base, tools)

    def _observe(usage: Usage) -> None:
        counter.observe(raw_estimate, usage.prompt_tokens)

    return _observe


def brain_manifest_config(config: ModelConfig, store: CalibrationStore) -> ManifestConfig:
    """The workspace map's limits for *config*'s brain tier (TD-5006, TD-5013).

    The map's share of the window becomes bytes at the brain endpoint's
    learned characters per token, keyed exactly as the turn's token
    counter keys it (``calibration_key``). *config* is the snapshot the
    session calls (its preset's, TD-1721, else the daemon's). The loop
    and the instruction inspector both size the map here. The figure is
    taken when the assembler is built and kept for the loop: a map that
    resized as the ratio moved would change the prompt prefix on every
    call.

    A brain tier on a loopback server with no slug in the config is
    resolved by discovery on the first turn (TD-1805), after a loop has
    built its assembler. Until then there is no key, so that loop's map
    is sized at the flat 4 characters per token.
    """
    brain = effective_tier(config, "brain", cu_heavy=False)
    return manifest_config_for_window(
        brain.context_window, chars_per_token=tier_chars_per_token(store, brain)
    )


async def _build_assistant_tool_call(
    session: Session,
    tool_calls: dict[int, dict[str, str | int]],
    dispatcher: ToolDispatcher | None = None,
) -> list[ProviderToolCall]:
    """Build provider-format tool calls from the accumulated deltas.

    Emits a ``ToolCall`` event for each tool call, classifying each via
    the dispatcher's classifier so the event carries the decision class
    (TD-702).  The classification is attached to the session's append-only
    event log — the audit record — *before* the tool executes.

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

        # Classify now (before execution) so the event carries the class.
        # Paths are joined to the workspace exactly as dispatch joins them,
        # or a relative path resolves against the daemon's cwd and the
        # event says C for a call dispatch runs as A (TD-5148).  The event
        # keeps the arguments the model sent.
        decision_class: Literal["A", "B", "C"] | None = None
        if dispatcher is not None:
            tool = dispatcher.registry.get(tc_name)
            if tool is not None and dispatcher.classifier is not None:
                joined = canonicalize_path_fields(tool, parsed_args, dispatcher.path_guard)
                request = build_decision_request(tool, joined)
                cls = (await dispatcher.classifier.classify(request)).decision_class
                decision_class = _to_literal(cls)

        await session.event_log.add(
            ToolCallEvent(
                session_id=session.id,
                tool_call_id=tc_id,
                name=tc_name,
                arguments=parsed_args,
                decision_class=decision_class,
                seq=1,
            )
        )
        mark_cu_tool(session, tc_name)
        await session.open_cu_session(tc_name)

    return provider_tool_calls


def _tool_message_content(result: Any) -> Any:
    """Tool output for the provider. Screenshots are a vision part, not text."""
    text = result.output
    png = getattr(result, "image_png", None)
    if not png:
        return text
    return [
        {"type": "text", "text": text},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")},
        },
    ]


async def _dispatch_and_append_results(
    dispatcher: ToolDispatcher,
    session: Session,
    messages: list[ChatMessage],
    tool_calls: dict[int, dict[str, str | int]],
) -> list[tuple[str, str, bool]]:
    """Execute tool calls and append their results to the conversation.

    Emits ``ToolResult`` events for each dispatch result. Returns one
    progress-guard note per result (name, canonical args, wrote a file).
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
        return []

    # Dispatch (parallel-safe tools run concurrently)
    results = await dispatcher.dispatch_many(dispatch_items, session)
    by_id = {item[0]: item for item in dispatch_items}
    notes: list[tuple[str, str, bool]] = []

    # Append results as tool role messages and emit events
    for r in results:
        await session.event_log.add(
            ToolResultEvent(
                session_id=session.id,
                tool_call_id=r.tool_call_id,
                status=r.status,
                output=r.output,
                truncated=r.truncated,
                error_code=r.error_code,
                diff=r.diff,
                allowed_by=list(r.allowed_by),
                transcript=list(r.transcript),
                seq=1,
            )
        )
        # One-time checkpoint degradation notices (TD-705), e.g. a
        # non-git workspace or pre-existing uncommitted changes.
        if r.checkpoint_notice is not None:
            await session.event_log.add(
                CheckpointNoticeEvent(
                    session_id=session.id,
                    code=r.checkpoint_notice.code,
                    message=r.checkpoint_notice.message,
                    seq=1,
                )
            )
        # Same one-time notice rail as checkpoints (TD-2104). The
        # message names memory; the event type is the existing rail.
        if r.memory_notice is not None:
            await session.event_log.add(
                CheckpointNoticeEvent(
                    session_id=session.id,
                    code=r.memory_notice.code,
                    message=r.memory_notice.message,
                    seq=1,
                )
            )

        messages.append(
            ChatMessage(
                role="tool",
                content=_tool_message_content(r),
                tool_call_id=r.tool_call_id,
            )
        )
        note_tool_result(session, r.name, r.status, diff=r.diff, output=r.output)
        item = by_id.get(r.tool_call_id)
        args: Mapping[str, Any] = item[2] if item is not None else {}
        notes.append(tool_note(r.name, args, status=r.status))
    record_autonomy_round(session, dispatch_items, results)
    await session.conversation_changed()
    return notes


async def dispatch_worker_child_tools(
    dispatcher: ToolDispatcher,
    session: Session,
    items: list[tuple[str, str, dict[str, Any]]],
) -> list[Any]:
    """Run child-worker tools through the same classifier gate (TD-4602).

    Lives here so the chokepoint inventory keeps seeing ``dispatch_many``
    only in ``loop.py`` / ``dispatch.py`` / ``dod.py``.  Results stay on
    *session* (the transient child), not the parent timeline.
    """
    return await dispatcher.dispatch_many(items, session)


def _assembler_for(
    session: Session, config: ModelConfig, calibration: CalibrationStore
) -> PromptAssembler:
    """The prompt assembler for the session's current root (TD-305)."""
    return PromptAssembler(
        session.workspace_path,
        claude_global_fallback=config.steering.claude_global_fallback,
        manifest_config=brain_manifest_config(
            session.config if session.config is not None else config, calibration
        ),
    )


def _load_import_approvals(session: Session) -> set[Path]:
    """Durable external-import approvals for the session's workspace (TD-505).

    A malformed config falls back to an empty allowlist (same
    tolerate-with-warning pattern as the daemon's config loaders) rather
    than crashing the session. An untrusted config approves nothing
    (TD-5113).
    """
    try:
        return set(trusted_imports(session.workspace_path, session.trust_store))
    except ConfigError as e:
        log.warning(
            "approved-imports config invalid; using empty allowlist",
            extra={
                "extra_fields": {"workspace_path": str(session.workspace_path), "error": str(e)}
            },
        )
        return set()


def _persist_import_approvals(session: Session, approved: set[Path]) -> None:
    """Save import approvals into the workspace config, keeping its trust (TD-5113).

    Raises:
        ConfigError: The write failed, or the config is untrusted
            (``UntrustedConfigError``): the approval then stays session-only.
    """
    store = session.trust_store
    # TD-5126: a worktree's untracked config is the project's file; its
    # copy follows once the project's is saved.
    home = config_home(session)
    if store is None:
        save_approved_imports(home, approved)
    else:
        # The key is rewritten whole and nothing else moves, so the write can
        # only add what it owns; it never exposes another section.
        store.write_through(
            home,
            lambda: save_approved_imports(home, approved),
            owns=("approved_external_imports",),
        )
    if session.worktree is not None and not session.worktree.config_tracked:
        refresh_config(session.worktree, store)


def session_boundary(session: Session) -> Boundary:
    """The wall the session's tools run inside: its root and its boundary config."""
    return Boundary(
        workspace_root=Path(session.workspace_path),
        writable_patterns=tuple(session.boundary_config.boundary.writable_paths),
        allowed_hosts=session.boundary_config.allowed_hosts,
        shell_read_only=(
            session.config.shell.read_only.entries() if session.config is not None else frozenset()
        ),
        # Read per call: Always allow and Revoke change the policy in place,
        # and a ``tst run --deny`` over a read guards the turn it came with
        # (TD-5118).
        read_guards=lambda: read_guards(with_run_rules(session.policy, session.run_permissions)),
        # TD-5123: read per call, so plan mode turned on or off mid-turn counts.
        plan_mode=lambda: session.router is not None and session.router.plan_mode,
    )


def _checkpointer_for(session: Session) -> Checkpointer:
    """Checkpoints on ``tst/session/<id>``, or the charter's auto branch (TD-4102)."""
    branch = None
    if session.autonomy and session.charter is not None:
        branch = auto_branch(session.charter.slug)
    # Log-Seq on every commit places it against the user turns (TD-5125).
    return Checkpointer(
        Path(session.workspace_path),
        session.id,
        branch=branch,
        log_seq=lambda: session.event_log.last_seq,
    )


def rebind_config(session: Session, dispatcher: ToolDispatcher) -> None:
    """Point *dispatcher* at the session's reloaded boundary and policy (TD-5113).

    The root is unchanged, so the checkpointer, ledger and memory committer
    stay: only the path guard, the classifier's wall and the policy gate
    read the config.
    """
    boundary = session_boundary(session)
    dispatcher.path_guard = PathGuard(boundary)
    if dispatcher.classifier is not None:
        dispatcher.classifier.rebind(boundary)
    dispatcher.policy = session.policy


def rebind_workspace(session: Session, dispatcher: ToolDispatcher) -> None:
    """Point every root-bound piece of *dispatcher* at the session's root (TD-1715).

    The loop wires these once, from the root the session opened in. After a
    live move the next tool call must canonicalize and judge its paths,
    checkpoint, commit memory, log its decision and resolve its approval in
    the new project, not quietly in the old one (review finding 17). The
    session's boundary config and policy are already reloaded by the time
    this runs. Pieces a caller left unset stay unset.
    """
    workspace = Path(session.workspace_path)
    rebind_config(session, dispatcher)
    if dispatcher.checkpointer is not None:
        dispatcher.checkpointer = _checkpointer_for(session)
    if dispatcher.memory_committer is not None:
        dispatcher.memory_committer = MemoryCommitter(workspace)
    if dispatcher.ledger is not None:
        dispatcher.ledger = DecisionLedger(session.workspace_path)
    dispatcher.workspace = workspace


async def _fit_prompt(
    session: Session,
    messages: list[ChatMessage],
    tier: TierName,
    tier_cfg: TierConfig,
    counter: TokenCounter,
    tool_definitions: list[ProviderToolDefinition] | None,
    *,
    overflow_retried: bool,
    max_prompt_images: int,
    clear_policy: ClearToolResultsConfig,
) -> None:
    """Bring the prompt *messages* make within the tier's budget, in place.

    Stale tool results first (TD-5115), then compaction (TD-405), then
    in-turn elision (TD-4839), each announced on the timeline, then the
    gauge and the image cap. Run before every model call, and again when
    a message sent mid-turn has just joined the prompt (TD-5112), so its
    tokens are counted too.
    """
    # Old tool results go before whole turns do (TD-5115): over the
    # same budget, earlier turns' results become one-line placeholders
    # in one batch that must land at 70% of it or under, so the turns
    # stay verbatim and the prefix holds until the next batch.
    # Compaction still runs if over.
    await _clear_stale_results(
        session, messages, tier, tier_cfg, counter, tool_definitions, clear_policy
    )
    # The task list (TD-5122) and the pinned plan (TD-5123) ride into the
    # summary: the calls that made them may be among the turns this drops.
    # TD-5128: the worker writes the summary when it can, and the files
    # the folded turns read or changed ride under it.
    summarizer = session.compaction_summarizer
    compacted, compaction = await compact_auto(
        messages,
        tier_cfg,
        counter,
        tool_definitions,
        carry=carry_for(session),
        summarizer=summarizer,
    )
    if compaction is not None:
        messages[:] = compacted
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

    # Near the budget, the next compaction's summary is written while
    # this turn runs, so the turn that reaches it does not wait (TD-5128).
    if summarizer is not None:
        summarizer.prefetch(messages, tier_cfg, counter, tool_definitions)

    # In-flight tool results sit past the compaction cut (TD-4839).
    # Once the prompt is over the same budget, or half of it
    # after a provider context overflow, elide their middles to
    # 70% of that budget (TD-5010), so the next results fit
    # without rewriting these. Messages stay, so a tool_calls
    # group is never split.
    elide_budget = max(1, budget_threshold(tier_cfg) // 2) if overflow_retried else None
    fitted, fit = fit_inflight_turn(
        messages, tier_cfg, counter, budget=elide_budget, tools=tool_definitions
    )
    if fit is not None:
        messages[:] = fitted
        await session.event_log.add(
            ContextCompacted(
                session_id=session.id,
                dropped_messages=fit.elided_results,
                kept_messages=fit.kept_messages,
                tokens_before=fit.tokens_before,
                tokens_after=fit.tokens_after,
                # A trim, not a compaction: the window labels it
                # apart, so a tool-heavy turn does not look like
                # it is compacting over and over (TD-5150).
                trigger="inflight_trim",
                seq=1,  # overwritten by the event log
            )
        )
        log.info(
            "in-turn context elided",
            extra={
                "extra_fields": {
                    "session_id": session.id,
                    "tier": tier,
                    "dropped_messages": fit.elided_results,
                    "tokens_before": fit.tokens_before,
                    "tokens_after": fit.tokens_after,
                    "counter_method": fit.counter_method,
                }
            },
        )
    # The title-bar gauge (TD-5109): what this request sends.
    await session.report_context_usage(estimate_tokens(messages, counter, tool_definitions)[0])

    dropped_images = cap_prompt_images(messages, max_prompt_images)
    if dropped_images:
        log.info(
            "capped prompt images",
            extra={
                "extra_fields": {
                    "session_id": session.id,
                    "dropped": dropped_images,
                    "max_prompt_images": max_prompt_images,
                }
            },
        )


async def agent_loop(
    session: Session,
    router: TierRouter,
    provider_factory: Callable[..., Awaitable[ProviderLike]],
    config: ModelConfig,
    tool_registry: ToolRegistry | None = None,
    tool_dispatcher: ToolDispatcher | None = None,
    prompt_assembler: PromptAssembler | None = None,
    audit_sink: ModelCallSink | None = None,
    calibration_store: CalibrationStore | None = None,
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
            (network or mock).  Receives the effective ``TierConfig`` for
            the call so a CU-heavy worker can use a different URL than
            the brain (TD-3903).  Zero-argument factories still work.
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
        audit_sink: Optional :class:`ModelCallSink` (TD-902).  When
            provided, every recorded model call is forwarded for audit
            persistence.  The sink only enqueues — it never blocks.
        calibration_store: Where learned token ratios live (TD-5007).
            ``None`` is the daemon-wide store, shared with every other
            session talking to the same endpoint.
    """
    # The daemon attaches the router before the loop runs (TD-1006); a
    # caller that only passed it here gets the same wiring, so the
    # classifier's plan-mode rule and ``exit_plan_mode`` (TD-5123) read
    # the router this loop routes by.
    if session.router is None:
        session.router = router
    # ── Conversation state ──────────────────────────────────────────
    # Learned token ratios (TD-5007). Taken before the assembler, which
    # sizes the workspace map from the brain endpoint's ratio (TD-5013).
    calibration = (
        calibration_store if calibration_store is not None else (process_calibration_store())
    )
    # The map is sized for the brain this session's calls go to: its own
    # preset snapshot when it has one (TD-1721), as at turn start and in
    # the instruction inspector, not the daemon's active preset.
    assembler = prompt_assembler or _assembler_for(session, config, calibration)
    embeddings_client = EmbeddingsClient.from_config(config)

    # External-import approvals (TD-505): approved paths are durable per
    # workspace; paths a person denied are session-scoped and not
    # re-prompted.  A deny-and-continue run's refusal is per turn (TD-5118).
    approved_imports = _load_import_approvals(session)
    denied_imports: set[Path] = set()

    # Conversation messages only; the system message is assembled per
    # turn below (TD-305).  Lives on the session so a fork can truncate
    # it (TD-1708).  This name is an alias — never rebind the list.
    messages = session.conversation
    tracker = CostTracker(config)
    # TD-1201: reachable from the daemon so get_instruction_stack can
    # report provider-observed cache state.
    session.cost_tracker = tracker
    # TD-902: feed every recorded model call to the audit writer. The
    # sink only enqueues — it can never block or fail the loop.
    if audit_sink is not None:

        def _forward(rec: CallRecord, is_classifier: bool) -> None:
            audit_sink.record_model_call(session.id, rec, is_classifier)

        tracker.add_listener(_forward)
    # Clients keyed by base_url so a remapped local worker (TD-3903) does
    # not reuse the remote brain client.  Resolved lazily on first use.
    # The key and timeouts are part of the key too, as in the daemon's own
    # cache: a backup on the same host with a second key, or other
    # timeouts, gets a client of its own (TD-5120).
    providers: dict[tuple[str, str, tuple[float | None, ...]], ProviderLike] = {}

    async def client_for(tier_cfg: TierConfig) -> ProviderLike:
        key = (
            tier_cfg.base_url,
            resolve_credential_id(tier_cfg) or "",
            tier_cfg.timeout_key(),
        )
        existing = providers.get(key)
        if existing is not None:
            return existing
        created = await _invoke_factory(provider_factory, tier_cfg)
        providers[key] = created
        return created

    async def _worker_completion(prompt: str) -> str:
        # Classifier and DoD polls stay on the active preset's worker,
        # not the CU remapped client — they are not a pixel loop (TD-3903).
        worker_cfg = config.tier("worker")
        worker_client = await client_for(worker_cfg)
        # The 8-token cap is the classifier's own (a one-letter verdict),
        # not the tier's; DoD polls and judgment calls share it. extra_body
        # still applies (TD-5008), with verdict_extra_body merged over it
        # (TD-5012): an 8-token answer ends inside a reasoning model's
        # thinking, so this is where that thinking is turned off. Both
        # only fill in, so temperature stays 0.
        request = ChatCompletionRequest(
            model=worker_cfg.require_slug(),
            messages=[ChatMessage(role="user", content=prompt)],
            max_tokens=_VERDICT_MAX_TOKENS,
            temperature=0.0,
            extra_body=worker_cfg.verdict_extras(),
        )
        resp = await worker_client.chat_completion(request)
        if isinstance(resp, ProviderError):
            raise RuntimeError(f"worker call failed: {resp.message}")
        cost = 0.0
        if resp.usage is not None:
            cost = tracker.record_classifier("worker", resp.usage, worker_cfg)
            # Classifier / DoD cost shows up in the meter too (TD-1006).
            await session.event_log.add(tracker.emit_cost_update(session.id))
        # The judgment seam reads this after the await.  DoD polls share
        # the callable and ignore the note.
        note_judgment_completion_cost(cost)
        text = content_as_text(resp.message.content)
        if not text.strip() and resp.finish_reason == "length":
            _warn_verdict_cap_once(worker_cfg, resolve_base_url(config, worker_cfg), request.model)
        return text

    async def _judgment_backend_for(cfg: JudgmentsConfig) -> JudgmentBackend:
        return await judgment_backend_for(cfg, _worker_completion)

    # Decision classifier chokepoint (TD-702/703, prime §2.6).  Every tool
    # call routes through it: the static rule table first; ambiguous cases
    # go to a worker-tier call (TD-703) that defaults to B, never A.  The
    # boundary is the session's workspace.  The worker call is a single-shot
    # completion on the worker tier's model, tracked as separate
    # classifier cost (it never touches the main turn accounting).
    if tool_dispatcher is not None:
        # Git entries on the read-only shell list wait for this (TD-5103).
        await probe_git()
        boundary = session_boundary(session)
        judgments_cfg = config.judgments
        if tool_dispatcher.classifier is None:

            def _on_judgment(record: JudgmentAudit) -> None:
                forward_classifier_judgment(
                    record,
                    session_id=session.id,
                    writer=audit_sink,
                    tracker=tracker,
                )

            tool_dispatcher.classifier = AmbiguousClassifier(
                static=DecisionClassifier(boundary),
                backend=await classifier_connector(judgments_cfg, _worker_completion),
                min_confidence=judgments_cfg.confidence_threshold,
                on_audit=_on_judgment,
            )

        # Judgment features gate individually and default off, so an
        # unconfigured run does not build a second connector.  A hosted
        # connector without its keychain key falls back to the worker
        # tier — fail toward working, never a block.
        needs_backend = (
            judgments_cfg.semantic_breaker
            or judgments_cfg.candidate_selection
            or judgments_cfg.verification
        )
        if needs_backend and session.judgment_backend is None:
            session.judgment_backend = await _judgment_backend_for(judgments_cfg)
            session.judgment_threshold = judgments_cfg.confidence_threshold
        if judgments_cfg.verification and tool_dispatcher.verifier is None:
            tool_dispatcher.verifier = ActuationVerifier(
                session.judgment_backend
                if session.judgment_backend is not None
                else WorkerChatJudgmentBackend(_worker_completion),
                threshold=judgments_cfg.confidence_threshold,
                max_state_chars=judgments_cfg.max_state_chars,
            )

        # Path boundary enforcement (TD-602): the same workspace boundary
        # backs the guard that refuses out-of-bounds path access.
        if tool_dispatcher.path_guard is None:
            tool_dispatcher.path_guard = PathGuard(boundary)

        # Checkpoint commits (TD-705 / TD-4102): successful path-bearing
        # mutations commit to ``tst/session/<id>`` interactively, or
        # ``tst/auto/<charter-slug>`` on an unattended run.  Interactive
        # non-git workspaces degrade inside the checkpointer; autonomy
        # refuses at start instead.
        if tool_dispatcher.checkpointer is None:
            tool_dispatcher.checkpointer = _checkpointer_for(session)

        # Memory HEAD commits (TD-2104): accepted memory writes land on
        # the workspace repo as ``tst: memory update``. Not the
        # checkpointer — that never touches HEAD.
        if tool_dispatcher.memory_committer is None:
            tool_dispatcher.memory_committer = MemoryCommitter(Path(session.workspace_path))

        # Decisions ledger (TD-704): Class A/B decisions that execute
        # append to .tst/autonomy/DECISIONS.md and emit decision_logged.
        if tool_dispatcher.ledger is None:
            tool_dispatcher.ledger = DecisionLedger(session.workspace_path)

        # Approval policy gate (TD-801/802): the workspace policy feeds
        # the dispatch gate; calls resolving to ``ask`` park on the
        # session, which owns the pending-approval futures — a client
        # disconnect leaves them parked and resumable (§2.5).
        if tool_dispatcher.policy is None:
            tool_dispatcher.policy = session.policy
        if tool_dispatcher.workspace is None:
            tool_dispatcher.workspace = Path(session.workspace_path)
        if tool_dispatcher.approval_handler is None:
            tool_dispatcher.approval_handler = session.request_approval
        # TD-5118: the turn's ``tst run`` rules join the policy gate.
        if tool_dispatcher.run_permissions_fn is None:
            tool_dispatcher.run_permissions_fn = lambda: session.run_permissions

    # TD-1715: a live move (between turns) rebinds everything above, the
    # prompt assembler and the import approvals to the new root, so the
    # next turn neither reads, judges, commits nor is steered in the old one.
    def _on_workspace_moved() -> None:
        nonlocal assembler
        if tool_dispatcher is not None:
            rebind_workspace(session, tool_dispatcher)
        if prompt_assembler is None:
            assembler = _assembler_for(session, config, calibration)
        # A delegate child shares the (rebound) dispatcher and reads the
        # session's root and boundary at spawn; the assembler is the only
        # root-bound piece the runtime holds a copy of.
        runtime = session.delegate_runtime
        if runtime is not None:
            runtime.assembler = assembler
        approved_imports.clear()
        approved_imports.update(_load_import_approvals(session))

    session.on_workspace_moved = _on_workspace_moved

    # TD-5113: a trust decision re-reads the config for the same root.
    # Only what the config feeds is rebound, so this is safe mid-turn.
    def _on_config_reloaded() -> None:
        if tool_dispatcher is not None:
            rebind_config(session, tool_dispatcher)
        approved_imports.clear()
        approved_imports.update(_load_import_approvals(session))

    session.on_config_reloaded = _on_config_reloaded

    # TD-4103: unattended runs poll definition-of-done after each turn.
    # Interactive sessions never attach a poller.
    if session.autonomy and session.charter is not None and session.dod_poller is None:
        session.dod_poller = make_dod_poller(
            session,
            dispatcher=tool_dispatcher,
            ask_worker=_worker_completion,
        )

    if session.autonomy:

        async def _note_class_b(event: DaemonEvent, _log: SessionEventLog) -> None:
            if isinstance(event, DecisionLogged) and event.decision_class == "B":
                session.autonomy_class_b = True

        session.event_log.subscribe(_note_class_b)  # type: ignore[arg-type]
        attach_drift_check(session, config=config, client_for=client_for, tracker=tracker)

    # Pre-compute tool definitions if we have a registry. Each model call
    # offers these less ``consult`` when its tier is not given it (TD-5119).
    all_tool_definitions: list[ProviderToolDefinition] | None = None
    # TD-5127: MCP schemas over the threshold stay out of the request
    # until ``tool_search`` loads them; it is offered only then.
    mcp_deferral: McpToolDeferral | None = None
    if tool_registry is not None:
        all_tool_definitions = tool_registry.to_provider_definitions()
        unusable = {"delegate"} if session.delegate_depth >= 1 else set()
        if tool_dispatcher is None or not tool_dispatcher.has_handler(TOOL_SEARCH_NAME):
            unusable.add(TOOL_SEARCH_NAME)
        all_tool_definitions = [
            d for d in all_tool_definitions if d.function is None or d.function.name not in unusable
        ]
        mcp_deferral = McpToolDeferral(tool_registry, config.mcp.tool_search)

    # Tracks the last steering prefix hash for change detection (TD-509).
    _last_prefix_hash: str | None = None

    # TD-503: path-scoped rules active at the last assembly, so a rule
    # newly matched by a touched file is announced in the timeline.
    _last_active_rules: set[str] | None = None

    # One token counter per endpoint and model (TD-405); encoders load
    # once. Each is scaled by what that endpoint reports (TD-5007), from
    # the ``calibration`` store taken above.
    _token_counters: dict[tuple[str, str], CalibratedTokenCounter] = {}

    # Cap enforcement (TD-707): the session start clock and per-call
    # iteration counter the caps are measured against.
    _session_start = time.time()
    _iterations = 0

    # TD-4602: bind the parent host so ``delegate`` can run a worker child
    # against this loop's provider, dispatcher, and remaining caps.
    if tool_dispatcher is not None:
        from .tools.delegate import DelegateRuntime

        session.delegate_runtime = DelegateRuntime(
            provider_factory=client_for,
            config=config,
            dispatcher=tool_dispatcher,
            assembler=assembler,
            session_start=_session_start,
        )

    # Tier visibility (TD-1006): slugs for the wire, and the last tier the
    # UI was told about so tier_state only fires on change.  Filled once
    # slug resolution has run (TD-1805), so the title bar is never told a
    # model the daemon has not settled on.
    _model_slugs: dict[str, str] = {}
    _last_reported_tier: TierName | None = None
    _last_reported_slugs: dict[str, str] | None = None
    _last_reported_hosts: dict[str, str] | None = None
    _last_reported_backups: dict[str, int] | None = None

    async def report_tier_state(
        tier: TierName, tier_cfg: TierConfig, cu_heavy: bool, backups: TurnBackups
    ) -> None:
        """Send ``tier_state`` when the tier, a slug, a host or a backup moved.

        TD-1006 / TD-1720: the title bar is told which tier, slug and host
        are live. Host changes (credential remap, CU worker) also emit so
        the pill stays honest, and so does a move to a backup endpoint,
        which names the backup for its tier (TD-5120).
        """
        nonlocal _model_slugs, _last_reported_tier, _last_reported_slugs
        nonlocal _last_reported_hosts, _last_reported_backups
        _model_slugs, hosts = backups.overlay(
            titlebar_slugs(config, cu_heavy=cu_heavy), titlebar_hosts(config, cu_heavy=cu_heavy)
        )
        positions = backups.positions()
        if (
            tier == _last_reported_tier
            and _model_slugs == _last_reported_slugs
            and hosts == _last_reported_hosts
            and positions == _last_reported_backups
        ):
            return
        _last_reported_tier = tier
        _last_reported_slugs = _model_slugs
        _last_reported_hosts = hosts
        _last_reported_backups = positions
        await session.event_log.add(
            TierState(
                session_id=session.id,
                tier=tier,
                override=router.override,
                model_slugs=_model_slugs,
                preset=config.active_preset,
                hosts=hosts,
                plan=router.plan_mode,
                vision=tier_cfg.vision,
                fallbacks=positions,
                seq=1,
            )
        )

    def counter_for(tier_cfg: TierConfig) -> CalibratedTokenCounter:
        """The calibrated counter for the model *tier_cfg* calls (TD-5007).

        Keyed by endpoint and slug, so a backup endpoint learns its own
        ratio and never borrows the tier's (TD-5120).
        """
        key = calibration_key(tier_cfg)
        counter = _token_counters.get(key)
        if counter is None:
            counter = CalibratedTokenCounter(
                make_token_counter(tier_cfg.require_slug()), calibration, key
            )
            _token_counters[key] = counter
        return counter

    # TD-5128: compaction summaries on the worker, here and for /compact.
    attach_summarizer(
        session,
        config,
        client_for,
        tracker,
        lambda: FileAccess(
            PathGuard(session_boundary(session)),
            read_guards(with_run_rules(session.policy, session.run_permissions)),
        ),
    )

    # ── Turn loop ───────────────────────────────────────────────────
    while not session.cancel_requested:
        # 1. Wait for user input
        queued = await session.wait_for_user_message()
        if queued is None:
            break  # session was cancelled
        # A stop that landed as the previous turn sealed must not abort
        # this one. A stop after this line belongs to this turn.
        session.acknowledge_stop_turn()
        # TD-5118: this turn's ``tst run`` rules, None for any other turn.
        session.run_permissions = queued.run_permissions
        # TD-5112: a message sent from here to turn_complete joins this
        # turn at its next tool-batch boundary.
        session.open_steering(queued.id)
        # TD-5124: ``/goal`` is TST Desk's own command, never a skill's.
        goal_command = None if session.autonomy else parse_goal_command(queued.display)
        if goal_command is None:
            apply_slash_skill(
                session,
                queued.display,
                claude_global_fallback=config.steering.claude_global_fallback,
            )

        # TD-1721: a preset switch while idle is applied on this turn.
        if session.config is not None:
            config = session.config
            tracker.bind_config(config)
            runtime = session.delegate_runtime
            if runtime is not None:
                runtime.config = config

        # Turn observability (TD-1713): mark the dequeue itself. The
        # existing "turn start" log lands after prompt assembly, so a
        # stall between dequeue and assembly was invisible in the logs
        # (2026-08-14). Queue depth is post-dequeue — messages the loop
        # still owes the user. Content stays out of the logs; its length
        # is enough to correlate with a report.
        log.info(
            "turn started",
            extra={
                "session_id": session.id,
                "queued_messages": session.pending_user_messages,
                "content_length": len(queued.display),
            },
        )

        provider_content = queued.provider
        if goal_command is not None and goal_command.kind == "set":
            # The goal is the task: the model gets it as a directive.
            provider_content = goal_provider_content(goal_command.text, queued.provider)
        # TD-5123: while planning, the message reaches the model with the
        # plan-mode note after it; the window shows it as typed.
        if router.plan_mode:
            provider_content = with_plan_note(provider_content)
        messages.append(ChatMessage(role="user", content=provider_content))
        await session.event_log.add(
            UserTurn(
                session_id=session.id,
                turn_id=queued.id,
                content=queued.display,
                run_permissions=queued.run_permissions,
                seq=1,
            )
        )
        if goal_command is not None and goal_command.kind == "set":
            await set_goal(session, goal_command.text, "set")
        await session.conversation_changed()
        # TD-5155: the turn is on disk, so the queue file lets it go.
        await session.user_turn_logged(queued)

        # 1a. Open the turn's accumulators (TD-1806).  Both live out here,
        #     at the turn boundary, because a turn is what they measure: a
        #     turn that makes a tool call spends several provider calls, and
        #     resetting per call reported the last leg's tokens and the last
        #     leg's duration as if they were the whole turn.  Per-call
        #     accounting is untouched — the ledger row and the cost_update
        #     ride on ``record()``, once per call (TD-1804).
        tracker.begin_turn()
        # consult's per-turn cap (TD-5119) counts from here.
        session.consult_tally.used = 0
        clear_turn_writes(session)
        turn_start = time.time()
        # TD-5118: imports a deny-and-continue run refused.  No person
        # answered, so they stay out for this turn only; the next turn,
        # whoever sends it, asks again.
        run_refused_imports: set[Path] = set()

        # TD-5124: ``/goal`` alone, ``/goal clear`` or a goal over the
        # limit is answered here, with no model call.
        if goal_command is not None and goal_command.kind != "set":
            note = await answer_goal_command(session, goal_command)
            await _show_progress_note(session, messages, note)
            await _emit_turn_complete(session, router.active_tier, turn_start, tracker)
            session.snapshot_branches()
            continue

        # 1b. Resolve any tier that leaves its slug unset (TD-1805).  This
        #     is the "first use" the story means: not import time, and not
        #     session open — an absent local model server must fail the
        #     *turn*, the way a missing key does (TD-1008), so the
        #     conversation survives, the user starts their server, and the
        #     next message goes through.  Once every slug is set the call
        #     is a dict scan, so no turn pays a second round-trip.
        try:
            await resolve_tier_slugs(config)
        except ModelDiscoveryError as e:
            messages.append(ChatMessage(role="assistant", content=f"I encountered an error: {e}"))
            await session.conversation_changed()
            await _emit_turn_complete(
                session,
                router.active_tier,
                turn_start,
                tracker,
                failed=True,
                error_code="model_unresolved",
            )
            log.warning(
                "turn failed: no model resolved for the endpoint",
                extra={
                    "extra_fields": {
                        "session_id": session.id,
                        "endpoint": e.endpoint,
                    }
                },
            )
            if session.autonomy:
                return
            continue
        _model_slugs = titlebar_slugs(config, cu_heavy=session_is_cu_heavy(session))

        # 2. Tool-call round-trip loop
        #    Each iteration: call provider → execute tool calls → loop
        #    until the model returns a text response.
        #    One overflow retry per user turn. Resetting this inside the
        #    loop would clear it on the continue that performs the retry.
        overflow_retried = False
        # Tiers this turn moved to a backup endpoint (TD-5120). Fresh per
        # user turn, so the next one starts on each tier's own model.
        backups = TurnBackups()
        guard = ProgressGuard()
        # TD-5124: the done-check when the model ends this turn.
        stop_check = TurnStopCheck(session, tool_dispatcher, _worker_completion)
        # Model calls this turn has made, and the last tool batch: where a
        # message sent mid-turn joins it (TD-5112).
        turn_step = 0
        last_batch: list[str] = []
        while True:
            if session.cancel_requested:
                break
            if session.stop_turn_requested:
                await _finish_stopped_turn(session, router.active_tier, turn_start, tracker)
                break
            # 2a. Determine active tier via router
            tier = router.record_turn_start()
            session.acting_tier = tier
            tool_definitions = offered_tools(all_tool_definitions, tier, config.consult)
            cu_heavy = session_is_cu_heavy(session)
            tier_cfg = effective_tier(config, tier, cu_heavy=cu_heavy)
            if tier == "worker" and cu_heavy and tier_cfg.slug is None:
                # First use of the remapped worker: discover or fail the
                # turn the same way an unresolved active-preset slug does.
                try:
                    tier_cfg.slug = await discover_model(tier_cfg.base_url, tier="worker")
                except ModelDiscoveryError as e:
                    messages.append(
                        ChatMessage(role="assistant", content=f"I encountered an error: {e}")
                    )
                    await session.conversation_changed()
                    await _emit_turn_complete(
                        session,
                        tier,
                        turn_start,
                        tracker,
                        failed=True,
                        error_code="model_unresolved",
                    )
                    log.warning(
                        "turn failed: no model resolved for the local worker",
                        extra={
                            "extra_fields": {
                                "session_id": session.id,
                                "endpoint": e.endpoint,
                            }
                        },
                    )
                    break
            # A tier that moved to a backup earlier in this turn stays on
            # it: a dead endpoint costs its retry budget once (TD-5120).
            backup_chain = backups.chain_for(config, tier, tier_cfg)
            tier_cfg = backup_chain.current
            await report_tier_state(tier, tier_cfg, cu_heavy, backups)

            # 2b. Assemble the per-tier system prompt in stable-prefix
            #     order (TD-305), gating external imports (TD-505): an
            #     import resolving outside the workspace parks the session
            #     for approval before the turn proceeds.  The gate loops
            #     because approving one file can reveal nested external
            #     imports (bounded by TD-504's max depth 4).
            while True:
                memory_block: str | None = None
                project_context: str | None = None
                if tier == "brain":
                    loaded = await load_memory_for_turn(
                        session.workspace_path,
                        queued.display,
                        embeddings_client,
                        load_global=session.load_global_memory,
                    )
                    session.last_memory = loaded
                    memory_block = loaded.block
                    from .context_pins import load_project_context

                    ctx = await asyncio.to_thread(
                        load_project_context,
                        Path(session.workspace_path),
                        config.project_context.token_budget,
                    )
                    project_context = ctx.block
                assembled = await assembler.assemble(
                    tier,
                    task=queued.display if tier == "worker" else None,
                    matched_paths=set(session.touched_paths),
                    memory=memory_block,
                    project_context=project_context if tier == "brain" else None,
                    approved_imports=frozenset(approved_imports),
                    denied_imports=frozenset(denied_imports | run_refused_imports),
                    loaded_skills=list(session.loaded_skills),
                )
                pending = [
                    p
                    for p in assembled.steering.pending_imports
                    if p not in denied_imports and p not in run_refused_imports
                ]
                if not pending:
                    break
                approved_any = False
                for path in pending:
                    outcome = await session.request_import_approval(path)
                    if outcome.approved:
                        approved_imports.add(path)
                        approved_any = True
                    else:
                        if outcome.unattended:
                            run_refused_imports.add(path)
                        else:
                            denied_imports.add(path)
                        log.warning(
                            "external import denied",
                            extra={
                                "extra_fields": {
                                    "session_id": session.id,
                                    "path": str(path),
                                    "unattended": outcome.unattended,
                                }
                            },
                        )
                if approved_any:
                    try:
                        _persist_import_approvals(session, approved_imports)
                    except ConfigError as e:
                        # The in-memory set still gates this session; only
                        # the durable write fails (e.g. the config file is
                        # malformed and cannot be round-tripped, or is not
                        # trusted yet: TD-5113).
                        log.warning(
                            "approved imports not persisted; approval is session-only",
                            extra={
                                "extra_fields": {
                                    "workspace_path": str(session.workspace_path),
                                    "error": str(e),
                                }
                            },
                        )

            # 2b.2 Path-scoped rule activation (TD-503).  The assembler
            #     marks a scoped rule active once its globs match a file
            #     the session has touched; a rule that newly activates
            #     mid-session is announced in the timeline so context
            #     changes are never silent.  The first assembly of the
            #     session is the baseline, not an activation.
            active_rules = {
                _rule_rel_path(session, s.path)
                for s in assembled.steering.sources
                if s.applies_to is not None and s.active
            }
            if _last_active_rules is not None:
                for rule_path in sorted(active_rules - _last_active_rules):
                    await session.event_log.add(
                        RuleActivated(
                            session_id=session.id,
                            rule_path=rule_path,
                            seq=1,  # overwritten by the event log
                        )
                    )
            _last_active_rules = active_rules

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
                        steering_tokens=assembled.steering_tokens,
                        source_count=len(assembled.steering.sources),
                        seq=1,  # overwritten by the event log
                    )
                )
                await session.event_log.add(
                    build_instruction_stack(
                        session.id,
                        assembled.steering,
                        seq=1,
                        last_cached_tokens=tracker.last_cached_prompt_tokens,
                        cache_observed=tracker.cache_observed,
                        memory=session.last_memory,
                        skills=list_workspace_skills(
                            session.workspace_path,
                            claude_global_fallback=config.steering.claude_global_fallback,
                        ),
                        loaded_skill_names=session.loaded_skills,
                    )
                )
                log.info(
                    "steering reloaded",
                    extra={
                        "extra_fields": {
                            "session_id": session.id,
                            "prefix_hash": assembled.prefix_hash,
                            "prefix_tokens": assembled.prefix_tokens,
                            "steering_tokens": assembled.steering_tokens,
                        }
                    },
                )
            _last_prefix_hash = assembled.prefix_hash

            # 2b.2 Context window guard (TD-405): when the estimated
            #     prompt approaches the tier's context window, compact
            #     older turns into a summary.  Cuts land where a turn
            #     started, so in-flight tool-call pairs are never split
            #     (TD-5112: a steer starts no turn).  Announced on the
            #     timeline, never silent.  The
            #     steering block and manifest were just re-read from
            #     disk at 2b, so instructions survive compaction.
            model_slug = tier_cfg.require_slug()
            counter = counter_for(tier_cfg)
            if mcp_deferral is not None:
                tool_definitions = mcp_deferral.offer(
                    tool_definitions,
                    session=session,
                    tier=tier,
                    tier_cfg=tier_cfg,
                    counter=counter,
                    messages=messages,
                )
            session.context_meter = ContextMeter(
                tier, tier_cfg, counter, tool_definitions, fallback=backup_chain.position
            )
            await _fit_prompt(
                session,
                messages,
                tier,
                tier_cfg,
                counter,
                tool_definitions,
                overflow_retried=overflow_retried,
                max_prompt_images=config.computer_use.max_prompt_images,
                clear_policy=config.clear_tool_results,
            )

            log.info(
                "turn start",
                extra={
                    "extra_fields": {
                        "session_id": session.id,
                        "tier": tier,
                        "model": model_slug,
                        "turn": router.turn_count,
                        "cache_prefix_hash": assembled.prefix_hash,
                        "cache_prefix_tokens": assembled.prefix_tokens,
                        # None until this endpoint has reported usage.
                        "token_ratio": counter.ratio,
                    }
                },
            )

            # 2c. Resolve provider lazily per endpoint. A missing keychain
            #     entry fails the *turn*, not the session (TD-1008): the
            #     conversation survives, the user stores a key, and the next
            #     message retries — client_for only caches on success.
            try:
                provider = await client_for(tier_cfg)
            except KeychainError as e:
                keychain_msg = f"I encountered an error: {e}"
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=keychain_msg,
                    )
                )
                await session.event_log.add(
                    AssistantDelta(
                        session_id=session.id,
                        delta=keychain_msg,
                        seq=1,
                    )
                )
                await session.conversation_changed()
                await _emit_turn_complete(
                    session,
                    tier,
                    turn_start,
                    tracker,
                    failed=True,
                    error_code="missing_api_key",
                )
                log.warning(
                    "turn failed: no API key in keychain",
                    extra={"extra_fields": {"session_id": session.id}},
                )
                if session.autonomy:
                    return
                break

            # 2c.5 Cap enforcement (TD-707).  Before every model call,
            #     a declared cap that is exceeded parks the session in a
            #     distinct fault state — a summary, not an approval
            #     request.  On resume the caps are re-checked; if the
            #     user raised them, the call proceeds.
            skip_all = bool(
                tool_dispatcher is not None
                and tool_dispatcher.skip_all_fn is not None
                and tool_dispatcher.skip_all_fn()
            )
            while True:
                if session.cancel_requested or session.stop_turn_requested:
                    break
                violation = _cap_violation(
                    session, tracker, _session_start, _iterations, skip_all=skip_all
                )
                if violation is None:
                    break
                if session.autonomy:
                    session.autonomy_stop_reason = violation
                    await deliver_wakeup(session)
                    return
                log.warning(
                    "cap pause",
                    extra={
                        "extra_fields": {
                            "session_id": session.id,
                            "summary": violation,
                        }
                    },
                )
                await session.pause_at_cap(violation)
                await session.wait_for_resume()
                if session.cancel_requested or session.stop_turn_requested:
                    break
            if session.cancel_requested:
                break
            if session.stop_turn_requested:
                await _finish_stopped_turn(session, tier, turn_start, tracker)
                break
            _iterations += 1
            if session.delegate_runtime is not None:
                session.delegate_runtime.iterations = _iterations

            # Interactive turns pause when a stretch of calls produces no
            # file write (TD-5002). Autonomy keeps the charter breakers.
            if not await _interactive_guard(
                guard,
                session,
                messages,
                provider,
                model_slug,
                tracker,
                tier,
                tier_cfg,
                turn_start,
                config,
                client_for,
                assembler,
                router,
                calibration,
                stop_check,
            ):
                break

            # 2c.6 A message sent while the last batch ran (or while the
            #     turn sat paused after it) goes in now, before the next
            #     model call (TD-5112). Stop and cancel were checked above.
            #     Only at a batch boundary: a retried call is not one.
            #     The prompt was fitted before the pause; fit it again
            #     with the steer in, so its tokens are counted and the
            #     results the turn got before it can still be elided.
            if last_batch:
                skills_before = list(session.loaded_skills)
                steered = await inject_steers(
                    session,
                    messages,
                    step=turn_step,
                    after_tool_call_ids=last_batch,
                    claude_global_fallback=config.steering.claude_global_fallback,
                )
                last_batch = []
                if session.loaded_skills != skills_before:
                    # A /skill in the steer: the prompt was assembled at
                    # 2b, before it loaded, so the call that reads the
                    # steer must get the skill too (TD-5160).
                    assembled = await assembler.assemble(
                        tier,
                        task=queued.display if tier == "worker" else None,
                        matched_paths=set(session.touched_paths),
                        memory=memory_block,
                        project_context=project_context if tier == "brain" else None,
                        approved_imports=frozenset(approved_imports),
                        denied_imports=frozenset(denied_imports),
                        loaded_skills=list(session.loaded_skills),
                    )
                    system = ChatMessage(role="system", content=assembled.text)
                    if messages and messages[0].role == "system":
                        messages[0] = system
                    else:
                        messages.insert(0, system)
                if steered:
                    await _fit_prompt(
                        session,
                        messages,
                        tier,
                        tier_cfg,
                        counter,
                        tool_definitions,
                        overflow_retried=overflow_retried,
                        max_prompt_images=config.computer_use.max_prompt_images,
                        clear_policy=config.clear_tool_results,
                    )

            # 2d. Call provider (streaming). Only this call teaches the
            #     token ratio (TD-5007); the progress check does not. A
            #     stall after output began is withdrawn from the window
            #     and the call is made once more (TD-5159).
            turn_step += 1
            reply = await _stream_turn_reply(
                provider,
                model_slug,
                messages,
                session,
                tool_definitions,
                tracker,
                tier,
                tier_cfg,
                counter,
                config,
            )

            # 2d.1 The tier's endpoint is down: once its retry budget is
            #     spent, the same request goes to the next backup in its
            #     `fallbacks`, and the turn stays there (TD-5120).
            if backup_wanted(reply) and len(backup_chain.models) > 1:
                moved = await _reply_with_backups(
                    reply,
                    backup_chain,
                    session=session,
                    provider=provider,
                    counter=counter,
                    messages=messages,
                    tool_definitions=tool_definitions,
                    tracker=tracker,
                    config=config,
                    client_for=client_for,
                    counter_for=counter_for,
                    overflow_retried=overflow_retried,
                    on_switch=functools.partial(
                        report_tier_state, tier, cu_heavy=cu_heavy, backups=backups
                    ),
                )
                reply, tier_cfg, counter, provider = (
                    moved.reply,
                    moved.tier_cfg,
                    moved.counter,
                    moved.provider,
                )
                model_slug = tier_cfg.require_slug()
            collected_content = reply.content
            tool_calls = reply.tool_calls
            failed = reply.failed
            error_msg = reply.error_msg
            error_code = reply.error_code
            raw_detail = reply.raw_detail
            finish_reason = reply.finish_reason
            reasoning_details = reply.reasoning_details

            # 2e. Handle failure
            if failed:
                # Cancel ends the session: no turn_complete, no failure row.
                # Stop seals this turn and leaves the session open.
                if session.cancel_requested:
                    break
                if session.stop_turn_requested or error_msg == "stopped":
                    partial = (collected_content or "").strip()
                    if partial:
                        messages.append(ChatMessage(role="assistant", content=partial))
                        await session.conversation_changed()
                    await _finish_stopped_turn(session, tier, turn_start, tracker)
                    break

                if error_code == "context_overflow":
                    # The raw body is for the log. Assistant text is built
                    # below from the configured window, after one tighter retry.
                    raw = redact_secrets(raw_detail or error_msg)[:2000]
                    log.warning(
                        "provider context overflow",
                        extra={
                            "extra_fields": {
                                "session_id": session.id,
                                "model": model_slug,
                                "context_window": tier_cfg.context_window,
                                "error": raw,
                            }
                        },
                    )
                    if not overflow_retried:
                        overflow_retried = True
                        continue

                router.record_failure()
                failure_msg = transcript_failure_text(
                    error_code,
                    error_msg,
                    model=model_slug,
                    context_window=tier_cfg.context_window,
                )
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=failure_msg,
                    )
                )
                # The message has to reach the event log too, not just the
                # persisted conversation: without a delta the chat pane shows
                # nothing at all on a failed turn — live or on replay — and a
                # turn that answers with silence reads as a hung app.
                await session.event_log.add(
                    AssistantDelta(
                        session_id=session.id,
                        delta=failure_msg,
                        seq=1,
                    )
                )
                await session.conversation_changed()
                await _emit_turn_complete(
                    session, tier, turn_start, tracker, failed=True, error_code=error_code
                )
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
                if session.autonomy:
                    return
                break  # exit tool-call loop, wait for next user message

            if not tool_calls and not (collected_content or "").strip():
                router.record_failure()
                empty_msg = (
                    "The model finished without a reply — it spent the "
                    "turn thinking, or hit the output limit mid-thought. "
                    "Start a new session if the context is already full."
                )
                messages.append(ChatMessage(role="assistant", content=empty_msg))
                await session.conversation_changed()
                await session.event_log.add(
                    AssistantDelta(
                        session_id=session.id,
                        delta=empty_msg,
                        seq=1,
                    )
                )
                await _emit_turn_complete(
                    session,
                    tier,
                    turn_start,
                    tracker,
                    failed=True,
                    error_code="empty_completion",
                )
                log.warning(
                    "turn failed: empty completion",
                    extra={
                        "extra_fields": {
                            "session_id": session.id,
                            "tier": tier,
                            "turn": router.turn_count,
                            "finish_reason": finish_reason,
                        }
                    },
                )
                if session.autonomy:
                    return
                break

            # 2e. Record success
            router.record_success()

            # A reply stopped at max_output_tokens (TD-5008): cut text is
            # kept; a cut tool call is never run or replayed as sent.
            cut_calls: dict[int, dict[str, str | int]] = {}
            if finish_reason == LENGTH_FINISH:
                cut_calls = {
                    index: call
                    for index, call in tool_calls.items()
                    if not arguments_complete(str(call["arguments"]))
                }
                tool_calls = {i: c for i, c in tool_calls.items() if i not in cut_calls}
                log_output_cap(
                    session_id=session.id,
                    tier=tier,
                    model=model_slug,
                    max_output_tokens=tier_cfg.max_output_tokens,
                    cut_calls=len(cut_calls),
                )

            # 2f. Append assistant response to conversation
            if tool_calls or cut_calls:
                provider_tool_calls = await _build_assistant_tool_call(
                    session, tool_calls, tool_dispatcher
                )
                provider_tool_calls += [
                    stub_call(str(call["id"]), str(call["name"])) for call in cut_calls.values()
                ]
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=None,
                        tool_calls=provider_tool_calls,
                        reasoning_details=reasoning_details,
                    )
                )
                # Answered now, so the model sees why and retries smaller.
                messages.extend(
                    cut_call_reply(str(call["id"]), tier_cfg.max_output_tokens)
                    for call in cut_calls.values()
                )
                await session.conversation_changed()

                # 2g. Execute tool calls via dispatcher (if available)
                if tool_dispatcher is not None:
                    # The historical 50_000 ceiling is the large-window cap.
                    # A smaller tier cannot absorb several results at that
                    # size, so the cap scales with the tokens still free
                    # in this prompt (TD-4839). Set on the dispatcher: the
                    # delegate child copies the attribute when it is built.
                    prefix_tokens, _ = estimate_tokens(messages, counter, tool_definitions)
                    tool_dispatcher.max_result_chars = tool_result_char_cap(
                        tier_cfg.context_window,
                        tier_cfg.max_output_tokens,
                        prefix_tokens,
                        effective_chars_per_token(counter),
                    )
                    # Yield control so the event loop can process cancellation
                    # between the ToolCall event emission and the dispatch.
                    # 0.05s is enough for the test to detect the event and cancel.
                    await asyncio.sleep(0.05)
                    notes = await _dispatch_and_append_results(
                        tool_dispatcher, session, messages, tool_calls
                    )
                    # Cancel ends the session. Stop seals the turn after the
                    # tool results are on the transcript, so the next
                    # message is not a dangling tool call.
                    if session.cancel_requested:
                        break
                    if session.stop_turn_requested:
                        await _finish_stopped_turn(session, tier, turn_start, tracker)
                        break
                    if not session.autonomy and session.delegate_depth < 1:
                        if (collected_content or "").strip():
                            guard.note_reply()
                        guard.note_tools(notes)
                    if session.autonomy and session.autonomy_class_c:
                        await _emit_turn_complete(session, tier, turn_start, tracker)
                        session.snapshot_branches()
                        if not await advance_autonomy(session):
                            return
                        break
                    # The batch is answered; a message sent while it ran
                    # joins before the next model call (2c.6, TD-5112).
                    last_batch = [call.id for call in provider_tool_calls]
                    # Loop back to call the provider again with tool results
                    continue
                # Without a dispatcher, fall through to emit turn_complete
            else:
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=collected_content or "",
                        reasoning_details=reasoning_details,
                    )
                )
                await session.conversation_changed()

            # 2h. No tool calls (or no dispatcher): the model ended the
            #     turn. The done-check may send it back to work (TD-5124).
            checked = await stop_check.at_turn_end(messages)
            if checked == "retry":
                continue
            if checked == "stopped":
                await _finish_stopped_turn(session, tier, turn_start, tracker)
                break
            await _emit_turn_complete(
                session,
                tier,
                turn_start,
                tracker,
                failed=checked == "failed",
                error_code="stop_check_failed" if checked == "failed" else None,
            )
            await maybe_verify_after_turn(
                session, config, tracker, client_for, assembler, calibration
            )
            session.snapshot_branches()
            if session.autonomy and not await advance_autonomy(session):
                return
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
                        # Without this, a 0.0 ratio reads the same whether
                        # the provider reported a miss or reported nothing
                        # — and only the first is a fact about the cache
                        # (TD-1811).  Turn-scoped like the ratio (TD-1814):
                        # last_cached_prompt_tokens is the last session
                        # call and would carry a previous turn's answer.
                        "cache_reported": tracker.turn_cache_reported(),
                    }
                },
            )
            break
