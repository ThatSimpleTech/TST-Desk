"""One-level worker subagent (TD-4602).

``delegate`` runs a transient worker child in-process.  The child is
daemon-owned (same process, same wall, same cards) but is not a second
top-level session and not a Hermes mesh: no grandchildren, no mailbox,
no peer agents, no extra window.

Child tool execution goes through :func:`tstd.loop.dispatch_worker_child_tools`
so the classifier chokepoint inventory stays in ``loop.py``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..config import ModelConfig, TierConfig
from ..context import PromptAssembler
from ..logging import get_logger
from ..provider import ChatCompletionRequest, ChatMessage, ProviderError
from ..session import Session
from .dispatch import ToolDispatcher
from .registry import Tool, ToolRegistry
from .results import HandlerRefusal

log = get_logger("tstd.delegate")

CHILD_MAX_ITERATIONS = 8
SUMMARY_CHAR_CAP = 2000
SUMMARY_TRUNCATION = "\n… [truncated: worker summary capped at 2000 characters]"

WORKER_CHILD_TOOLS: tuple[str, ...] = (
    "fs_read",
    "fs_list",
    "fs_write",
    "fs_edit",
    "shell",
)


@dataclass
class DelegateRuntime:
    """Parent-loop host bound so ``delegate`` can spawn a worker child.

    ``iterations`` is mutated by the parent loop after each parent model
    call so the child sees remaining ``max_iterations``.
    """

    provider_factory: Callable[..., Awaitable[Any]]
    config: ModelConfig
    dispatcher: ToolDispatcher
    assembler: PromptAssembler
    session_start: float
    iterations: int = 0


def register_delegate_tools(registry: ToolRegistry) -> None:
    """Register the builtin ``delegate`` tool. Call from ``create_registry``."""
    registry.register(
        Tool(
            name="delegate",
            description=(
                "Run a one-level worker child on a focused task. The child "
                "uses the worker model and filesystem/shell tools only; it "
                "cannot delegate further. Returns a capped summary."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task the worker child should complete",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional extra context for the child",
                    },
                },
                "required": ["task"],
            },
            side_effect_class="ask",
            parallel_safe=False,
            path_fields=(),
            host_fields=(),
        )
    )


def register_delegate_handlers(dispatcher: ToolDispatcher) -> None:
    """Register the ``delegate`` handler. Call from ``register_builtin_handlers``."""
    if dispatcher.registry.get("delegate") is None:
        raise KeyError("delegate tool must be registered before its handler")
    dispatcher.register_handler("delegate", handle_delegate)


def cap_worker_summary(text: str, limit: int = SUMMARY_CHAR_CAP) -> str:
    """Return *text* truncated to *limit* characters with a marker."""
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(SUMMARY_TRUNCATION))
    return text[:keep] + SUMMARY_TRUNCATION


def worker_child_registry(parent: ToolRegistry) -> ToolRegistry:
    """Copy fs/shell tools from *parent*. Never includes ``delegate``."""
    child = ToolRegistry()
    for name in WORKER_CHILD_TOOLS:
        tool = parent.get(name)
        if tool is not None:
            child.register(tool)
    return child


async def handle_delegate(
    session: object,
    task: str,
    context: str = "",
    tool_call_id: str = "",
) -> str:
    """``delegate`` handler — run a one-level worker child."""
    if not isinstance(session, Session):
        raise HandlerRefusal("delegate_no_session", "Error: delegate requires a session")
    if session.delegate_depth >= 1:
        raise HandlerRefusal(
            "delegate_nested",
            "Refused: delegate cannot nest (no grandchildren).",
        )
    runtime = session.delegate_runtime
    if not isinstance(runtime, DelegateRuntime):
        raise HandlerRefusal(
            "delegate_unbound",
            "Error: delegate runtime is not bound on this session",
        )
    return await run_worker_child(parent=session, task=task, context=context)


async def run_worker_child(*, parent: Session, task: str, context: str = "") -> str:
    """Slim worker loop: assemble, complete, dispatch, repeat until text or cap.

    Does not call ``agent_loop`` — no autonomy overlay, no CU episode, no
    second title-bar session.  Cost records land on the parent tracker
    with ``source="worker"``.
    """
    if parent.delegate_depth >= 1:
        raise HandlerRefusal(
            "delegate_nested",
            "Refused: delegate cannot nest (no grandchildren).",
        )
    runtime = parent.delegate_runtime
    if not isinstance(runtime, DelegateRuntime):
        raise HandlerRefusal(
            "delegate_unbound",
            "Error: delegate runtime is not bound on this session",
        )

    from ..cost import CostTracker
    from ..loop import _cap_violation, dispatch_worker_child_tools

    tracker = parent.cost_tracker
    if tracker is None:
        tracker = CostTracker(runtime.config)
        parent.cost_tracker = tracker

    parent_dispatcher = runtime.dispatcher
    child_registry = worker_child_registry(parent_dispatcher.registry)
    child_dispatcher = ToolDispatcher(
        child_registry,
        max_result_chars=parent_dispatcher.max_result_chars,
        classifier=parent_dispatcher.classifier,
        path_guard=parent_dispatcher.path_guard,
        checkpointer=parent_dispatcher.checkpointer,
        memory_committer=parent_dispatcher.memory_committer,
        ledger=parent_dispatcher.ledger,
        policy=parent_dispatcher.policy if parent_dispatcher.policy is not None else parent.policy,
        approval_handler=parent_dispatcher.approval_handler,
        workspace=parent_dispatcher.workspace,
    )
    child_dispatcher.skip_all_fn = parent_dispatcher.skip_all_fn
    child_dispatcher.autonomy_fn = parent_dispatcher.autonomy_fn
    child_dispatcher.on_class_c = parent_dispatcher.on_class_c
    for name in WORKER_CHILD_TOOLS:
        handler = parent_dispatcher._handlers.get(name)
        if handler is not None:
            child_dispatcher.register_handler(name, handler)

    child = Session(parent.workspace_path)
    child.delegate_depth = 1
    child.parent_id = parent.id
    child.boundary_config = parent.boundary_config
    child.policy = parent.policy
    child.cost_tracker = tracker
    child.touched_paths = parent.touched_paths
    child.autonomy = False

    caps = parent.boundary_config.caps
    remaining = max(0, caps.max_iterations - runtime.iterations)
    child_limit = min(CHILD_MAX_ITERATIONS, remaining)
    last_text = ""

    user_blob = task.strip()
    if context.strip():
        user_blob = f"{user_blob}\n\nContext:\n{context.strip()}"
    user_blob = (
        "You are a one-level worker. You cannot call delegate. "
        "Complete the task, then reply with a concise summary and no "
        f"further tool calls.\n\nTask:\n{user_blob}"
    )

    assembled = await runtime.assembler.assemble(
        "worker",
        task=user_blob,
        matched_paths=set(parent.touched_paths),
    )
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=assembled.text),
        ChatMessage(role="user", content=user_blob),
    ]

    worker_cfg = runtime.config.tier("worker")
    provider = await _provider_for(runtime.provider_factory, worker_cfg)
    tool_defs = child_registry.to_provider_definitions()

    if child_limit == 0:
        return cap_worker_summary("Worker stopped: parent iteration cap.")

    for child_i in range(child_limit):
        if parent.cancel_requested:
            return cap_worker_summary(last_text or "Worker stopped: parent cancelled.")
        # Child never inherits skip-all for spend: a delegated worker
        # must not keep billing after the parent cap is already hit.
        violation = _cap_violation(
            parent,
            tracker,
            runtime.session_start,
            runtime.iterations + child_i,
            skip_all=False,
        )
        if violation is not None:
            return cap_worker_summary(last_text or f"Worker stopped: {violation}")

        request = ChatCompletionRequest(
            model=worker_cfg.require_slug(),
            messages=list(messages),
            tools=tool_defs or None,
            stream=False,
        )
        resp = await provider.chat_completion(request)
        if isinstance(resp, ProviderError):
            return cap_worker_summary(last_text or f"Worker stopped: {resp.message}")
        if resp.usage is not None:
            tracker.record("worker", resp.usage, worker_cfg, source="worker")
            await parent.event_log.add(tracker.emit_cost_update(parent.id))

        tool_calls = resp.message.tool_calls or []
        content = resp.message.content or ""
        if content.strip():
            last_text = content

        if not tool_calls:
            finished = content.strip() or last_text or "Worker finished with no summary."
            return cap_worker_summary(finished)

        messages.append(resp.message)
        items: list[tuple[str, str, dict[str, Any]]] = []
        for tc in tool_calls:
            name = tc.function.name if tc.function is not None else ""
            raw = tc.function.arguments if tc.function is not None else "{}"
            parsed: dict[str, Any]
            try:
                loaded = json.loads(raw) if raw else {}
                parsed = loaded if isinstance(loaded, dict) else {"_raw": raw}
            except json.JSONDecodeError:
                parsed = {"_raw": raw}
            items.append((tc.id, name, parsed))

        results = await dispatch_worker_child_tools(child_dispatcher, child, items)
        for r in results:
            messages.append(ChatMessage(role="tool", content=r.output, tool_call_id=r.tool_call_id))

    return cap_worker_summary(last_text or "Worker stopped: iteration cap.")


async def _provider_for(
    factory: Callable[..., Awaitable[Any]],
    tier_cfg: TierConfig,
) -> Any:
    """Call *factory* with the worker tier; zero-arg factories still work."""
    try:
        return await factory(tier_cfg)
    except TypeError:
        return await factory()
