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
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from .audit_writer import ModelCallSink
from .autonomy import (
    AmbiguousClassifier,
    Boundary,
    Checkpointer,
    DecisionClass,
    DecisionClassifier,
    DecisionLedger,
)
from .autonomy.breakers import record_autonomy_round
from .autonomy.checkpoint import auto_branch
from .autonomy.dod import make_dod_poller
from .autonomy.runner import advance_autonomy
from .autonomy.supervisor import attach_drift_check
from .autonomy.verify import (
    clear_turn_writes,
    maybe_verify_after_turn,
    note_tool_result,
)
from .autonomy.wakeup import deliver_wakeup
from .compaction import maybe_compact
from .config import ConfigError, ModelConfig, ModelDiscoveryError, TierConfig
from .context import PromptAssembler
from .context.embeddings import EmbeddingsClient, load_memory_for_turn
from .context.stack import build_instruction_stack
from .context.tokens import TokenCounter, make_token_counter
from .cost import CallRecord, CostTracker
from .discovery import discover_model, resolve_tier_slugs
from .keychain import KeychainError
from .local_worker import (
    effective_tier,
    mark_cu_tool,
    session_is_cu_heavy,
    titlebar_hosts,
    titlebar_slugs,
)
from .logging import get_logger
from .memory_commit import MemoryCommitter
from .policy import load_approved_imports, save_approved_imports
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
)
from .provider import (
    ToolCall as ProviderToolCall,
)
from .provider import (
    ToolDefinition as ProviderToolDefinition,
)
from .router import TierName, TierRouter
from .session import Session, SessionEventLog
from .tools import ToolDispatcher, ToolRegistry
from .tools.boundary import PathGuard
from .tools.dispatch import build_decision_request


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


async def _stream_and_parse(
    provider: ProviderLike,
    model_slug: str,
    messages: list[ChatMessage],
    session: Session,
    tool_definitions: list[ProviderToolDefinition] | None,
    tracker: CostTracker,
    tier: TierName,
    tier_cfg: Any,
) -> tuple[str, dict[int, dict[str, str | int]], bool, str, str | None]:
    """Call the provider, stream deltas, and accumulate tool calls.

    Returns:
        A tuple of ``(collected_content, tool_calls, failed, error_msg,
        error_code)``. ``error_code`` is the provider's typed code (e.g.
        ``auth_failed``) so clients can key tailored copy off it (TD-1008).
    """
    collected_content = ""
    tool_calls: dict[int, dict[str, str | int]] = {}
    failed = False
    error_msg = ""
    error_code: str | None = None

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

    async for chunk in _stream_turn(provider, model_slug, messages, tool_definitions):
        if session.cancel_requested:
            failed, error_msg = True, "cancelled"
            break

        if isinstance(chunk, ProviderError):
            failed, error_msg, error_code = True, chunk.message, chunk.code
            break

        # Stream reasoning delta (TD-1901).  Emitted, never accumulated:
        # `collected_content` becomes the assistant message replayed to the
        # provider on the next round trip, and feeding a model its own
        # scratchpad back as something it said is both wrong and paid for.
        # A reasoning model spends minutes here, so this is also the only
        # sign of life the window gets before the answer starts.
        if chunk.delta.reasoning:
            await session.event_log.add(
                AssistantReasoning(
                    session_id=session.id,
                    delta=chunk.delta.reasoning,
                    seq=1,
                )
            )

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

        if chunk.usage is not None:
            usage = chunk.usage

    # Tokens the provider reported are recorded even when the call ended
    # badly — they were spent either way, and a cancelled or failed call
    # that bills nothing is the one way an audit ledger can understate real
    # consumption.
    if usage is not None:
        tracker.record(tier, usage, tier_cfg)
        # TD-1006: the cost meter updates as costs accrue — one
        # cost_update per recorded call, not one per turn.
        await session.event_log.add(tracker.emit_cost_update(session.id))

    return collected_content, tool_calls, failed, error_msg, error_code


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
        decision_class: Literal["A", "B", "C"] | None = None
        if dispatcher is not None:
            tool = dispatcher.registry.get(tc_name)
            if tool is not None and dispatcher.classifier is not None:
                request = build_decision_request(tool, dict(parsed_args))
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
                error_code=r.error_code,
                diff=r.diff,
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
                content=r.output,
                tool_call_id=r.tool_call_id,
            )
        )
        note_tool_result(session, r.name, r.status, diff=r.diff, output=r.output)
    record_autonomy_round(session, dispatch_items, results)
    await session.conversation_changed()


async def agent_loop(
    session: Session,
    router: TierRouter,
    provider_factory: Callable[..., Awaitable[ProviderLike]],
    config: ModelConfig,
    tool_registry: ToolRegistry | None = None,
    tool_dispatcher: ToolDispatcher | None = None,
    prompt_assembler: PromptAssembler | None = None,
    audit_sink: ModelCallSink | None = None,
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
    """
    # ── Conversation state ──────────────────────────────────────────
    assembler = prompt_assembler or PromptAssembler(session.workspace_path)
    embeddings_client = EmbeddingsClient.from_config(config)

    # External-import approvals (TD-505): approved paths are durable per
    # workspace; denied paths are session-scoped and not re-prompted.  A
    # malformed config falls back to an empty allowlist (same
    # tolerate-with-warning pattern as the daemon's config loaders) rather
    # than crashing the session at loop start.
    try:
        approved_imports: set[Path] = set(load_approved_imports(session.workspace_path))
    except ConfigError as e:
        log.warning(
            "approved-imports config invalid; using empty allowlist",
            extra={
                "extra_fields": {
                    "workspace_path": str(session.workspace_path),
                    "error": str(e),
                }
            },
        )
        approved_imports = set()
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
    providers: dict[str, ProviderLike] = {}

    async def client_for(tier_cfg: TierConfig) -> ProviderLike:
        key = tier_cfg.base_url
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
        request = ChatCompletionRequest(
            model=worker_cfg.require_slug(),
            messages=[ChatMessage(role="user", content=prompt)],
            max_tokens=8,
            temperature=0.0,
        )
        resp = await worker_client.chat_completion(request)
        if isinstance(resp, ProviderError):
            raise RuntimeError(f"worker call failed: {resp.message}")
        if resp.usage is not None:
            tracker.record_classifier("worker", resp.usage, worker_cfg)
            # Classifier / DoD cost shows up in the meter too (TD-1006).
            await session.event_log.add(tracker.emit_cost_update(session.id))
        return resp.message.content or ""

    # Decision classifier chokepoint (TD-702/703, prime §2.6).  Every tool
    # call routes through it: the static rule table first; ambiguous cases
    # go to a worker-tier call (TD-703) that defaults to B, never A.  The
    # boundary is the session's workspace.  The worker call is a single-shot
    # completion on the worker tier's model, tracked as separate
    # classifier cost (it never touches the main turn accounting).
    if tool_dispatcher is not None:
        boundary = Boundary(
            workspace_root=Path(session.workspace_path),
            writable_patterns=tuple(session.boundary_config.boundary.writable_paths),
            allowed_hosts=session.boundary_config.allowed_hosts,
        )
        if tool_dispatcher.classifier is None:
            tool_dispatcher.classifier = AmbiguousClassifier(
                static=DecisionClassifier(boundary),
                call_worker=_worker_completion,
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
            branch = None
            if session.autonomy and session.charter is not None:
                branch = auto_branch(session.charter.slug)
            tool_dispatcher.checkpointer = Checkpointer(
                Path(session.workspace_path),
                session.id,
                branch=branch,
            )

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

    # Pre-compute tool definitions if we have a registry
    tool_definitions: list[ProviderToolDefinition] | None = None
    if tool_registry is not None:
        tool_definitions = tool_registry.to_provider_definitions()

    # Tracks the last steering prefix hash for change detection (TD-509).
    _last_prefix_hash: str | None = None

    # TD-503: path-scoped rules active at the last assembly, so a rule
    # newly matched by a touched file is announced in the timeline.
    _last_active_rules: set[str] | None = None

    # One token counter per model slug (TD-405); encoders load once.
    _token_counters: dict[str, TokenCounter] = {}

    # Cap enforcement (TD-707): the session start clock and per-call
    # iteration counter the caps are measured against.
    _session_start = time.time()
    _iterations = 0

    # Tier visibility (TD-1006): slugs for the wire, and the last tier the
    # UI was told about so tier_state only fires on change.  Filled once
    # slug resolution has run (TD-1805), so the title bar is never told a
    # model the daemon has not settled on.
    _model_slugs: dict[str, str] = {}
    _last_reported_tier: TierName | None = None
    _last_reported_slugs: dict[str, str] | None = None
    _last_reported_hosts: dict[str, str] | None = None

    # ── Turn loop ───────────────────────────────────────────────────
    while not session.cancel_requested:
        # 1. Wait for user input
        user_content = await session.wait_for_user_message()
        if user_content is None:
            break  # session was cancelled

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
                "content_length": len(user_content),
            },
        )

        messages.append(ChatMessage(role="user", content=user_content))
        await session.event_log.add(
            UserTurn(
                session_id=session.id,
                turn_id=str(uuid.uuid4()),
                content=user_content,
                seq=1,
            )
        )
        await session.conversation_changed()

        # 1a. Open the turn's accumulators (TD-1806).  Both live out here,
        #     at the turn boundary, because a turn is what they measure: a
        #     turn that makes a tool call spends several provider calls, and
        #     resetting per call reported the last leg's tokens and the last
        #     leg's duration as if they were the whole turn.  Per-call
        #     accounting is untouched — the ledger row and the cost_update
        #     ride on ``record()``, once per call (TD-1804).
        tracker.begin_turn()
        clear_turn_writes(session)
        turn_start = time.time()

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
        while True:
            # 2a. Determine active tier via router
            tier = router.record_turn_start()
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
            _model_slugs = titlebar_slugs(config, cu_heavy=cu_heavy)
            _hosts = titlebar_hosts(config, cu_heavy=cu_heavy)
            if (
                tier != _last_reported_tier
                or _model_slugs != _last_reported_slugs
                or _hosts != _last_reported_hosts
            ):
                _last_reported_tier = tier
                _last_reported_slugs = _model_slugs
                _last_reported_hosts = _hosts
                # TD-1006 / TD-1720: tell the title bar which tier, slug,
                # and host are live. Host changes (credential remap, CU
                # worker) also emit so the pill stays honest.
                await session.event_log.add(
                    TierState(
                        session_id=session.id,
                        tier=tier,
                        override=router.override,
                        model_slugs=_model_slugs,
                        preset=config.active_preset,
                        hosts=_hosts,
                        seq=1,
                    )
                )

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
                        user_content,
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
                    task=user_content if tier == "worker" else None,
                    matched_paths=set(session.touched_paths),
                    memory=memory_block,
                    project_context=project_context if tier == "brain" else None,
                    approved_imports=frozenset(approved_imports),
                    denied_imports=frozenset(denied_imports),
                )
                pending = [p for p in assembled.steering.pending_imports if p not in denied_imports]
                if not pending:
                    break
                approved_any = False
                for path in pending:
                    outcome = await session.request_import_approval(path)
                    if outcome.approved:
                        approved_imports.add(path)
                        approved_any = True
                    else:
                        denied_imports.add(path)
                        log.warning(
                            "external import denied",
                            extra={
                                "extra_fields": {
                                    "session_id": session.id,
                                    "path": str(path),
                                }
                            },
                        )
                if approved_any:
                    try:
                        save_approved_imports(session.workspace_path, approved_imports)
                    except ConfigError as e:
                        # The in-memory set still gates this session; only
                        # the durable write fails (e.g. the config file is
                        # malformed and cannot be round-tripped).
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
            #     older turns into a summary.  Cuts land at user-message
            #     boundaries, so in-flight tool-call pairs are never
            #     split.  Announced on the timeline, never silent.  The
            #     steering block and manifest were just re-read from
            #     disk at 2b, so instructions survive compaction.
            model_slug = tier_cfg.require_slug()
            counter = _token_counters.get(model_slug)
            if counter is None:
                counter = make_token_counter(model_slug)
                _token_counters[model_slug] = counter
            compacted, compaction = maybe_compact(messages, tier_cfg, counter)
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
            _iterations += 1

            # 2d. Call provider (streaming)
            collected_content, tool_calls, failed, error_msg, error_code = await _stream_and_parse(
                provider,
                model_slug,
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
                failure_msg = f"I encountered an error: {error_msg}"
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
                        }
                    },
                )
                if session.autonomy:
                    return
                break

            # 2e. Record success
            router.record_success()

            # 2f. Append assistant response to conversation
            if tool_calls:
                provider_tool_calls = await _build_assistant_tool_call(
                    session, tool_calls, tool_dispatcher
                )
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=None,
                        tool_calls=provider_tool_calls,
                    )
                )
                await session.conversation_changed()

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
                    if session.autonomy and session.autonomy_class_c:
                        await _emit_turn_complete(session, tier, turn_start, tracker)
                        session.snapshot_branches()
                        if not await advance_autonomy(session):
                            return
                        break
                    # Loop back to call the provider again with tool results
                    continue
                # Without a dispatcher, fall through to emit turn_complete
            else:
                messages.append(ChatMessage(role="assistant", content=collected_content or ""))
                await session.conversation_changed()

            # 2h. No tool calls (or no dispatcher) — turn is complete
            await _emit_turn_complete(session, tier, turn_start, tracker)
            await maybe_verify_after_turn(session, config, tracker, client_for, assembler)
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
