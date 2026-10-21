"""One-level worker subagent (TD-4602).

``delegate`` runs a transient worker child in-process.  The child is
daemon-owned (same process, same wall, same cards) but is not a second
top-level session and not a Hermes mesh: no grandchildren, no mailbox,
no peer agents, no extra window.

Child tool execution goes through :func:`tstd.loop.dispatch_worker_child_tools`
so the classifier chokepoint inventory stays in ``loop.py``.

Two kinds of child (TD-5117): ``worker`` reads, writes and runs shell;
``research`` only reads files and the web. Either returns a summary
capped in tokens, and its transcript goes to the window on the
``delegate`` result, never into the parent's context. In plan mode
``research`` is the default and ``kind: worker`` is refused (TD-5123).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from ..config import ModelConfig, TierConfig
from ..context import PromptAssembler
from ..context.tokens import make_token_counter
from ..logging import get_logger
from ..output_cap import (
    LENGTH_FINISH,
    cut_call_reply,
    log_output_cap,
    split_cut_calls,
    stub_call,
)
from ..prompt_images import cap_prompt_images
from ..protocol import WorkerStep
from ..provider import ChatCompletionRequest, ChatMessage, ProviderError, ToolCall
from ..session import Session
from ..worktree import SessionWorktree
from .delegate_child import (
    CHILD_TOOLS,
    CONSULT_TOOL_NAME,
    DELEGATE_KINDS,
    RESEARCH_CHILD_TOOLS,
    SUMMARY_TOKEN_CAP,
    WORKER_CHILD_TOOLS,
    DelegateKind,
    TranscriptRecorder,
    cap_worker_summary,
    child_preamble,
    resolve_kind,
)
from .delegate_worktree import bind_child_root, close_child_tree, open_child_tree, worktree_brief
from .dispatch import ToolDispatcher
from .registry import Tool, ToolRegistry
from .results import HandlerFailed, HandlerOutput, HandlerRefusal

# The child's kinds, tools and cap live in ``delegate_child``; they are
# re-exported so callers keep one import path.
__all__ = [
    "CHILD_MAX_ITERATIONS",
    "RESEARCH_CHILD_TOOLS",
    "SUMMARY_TOKEN_CAP",
    "WORKER_CHILD_TOOLS",
    "DelegateRuntime",
    "cap_worker_summary",
    "handle_delegate",
    "register_delegate_handlers",
    "register_delegate_tools",
    "run_worker_child",
    "worker_child_registry",
]

log = get_logger("tstd.delegate")

CHILD_MAX_ITERATIONS = 8


@dataclass
class DelegateRuntime:
    """Parent-loop host bound so ``delegate`` can spawn a worker child.

    ``web_fetch``'s extract pass reaches the worker tier and the result
    cap through it too (TD-5114).  ``iterations`` is mutated by the parent
    loop after each parent model call so the child sees remaining
    ``max_iterations``.
    """

    provider_factory: Callable[..., Awaitable[Any]]
    config: ModelConfig
    dispatcher: ToolDispatcher
    assembler: PromptAssembler
    session_start: float
    iterations: int = 0
    # The session whose meter shows this runtime's spend. A delegate
    # child's ``consult`` (TD-5119) lands on the parent's ``cost_update``;
    # ``None`` is the session the runtime is bound to.
    meter: Session | None = None


def register_delegate_tools(registry: ToolRegistry) -> None:
    """Register the builtin ``delegate`` tool. Call from ``create_registry``."""
    registry.register(
        Tool(
            name="delegate",
            description=(
                "Run a one-level child on the worker model for a focused "
                "task; it cannot delegate further and returns a capped "
                "summary, so its reading stays out of your context. "
                "kind 'worker' (the default) can read, write files and run "
                "shell. kind 'research' is read-only: fs_read, fs_list, "
                "fs_grep, web_search and web_fetch. Either may also consult "
                "when it is on for the worker tier. Use research to explore "
                "code or the web. In plan mode only research runs. "
                "worktree: true runs the child in its own git worktree from "
                "the last commit (your uncommitted edits are not in it); "
                "its changes come back on a branch for you to merge."
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
                    "kind": {
                        "type": "string",
                        "enum": list(DELEGATE_KINDS),
                        "description": (
                            "worker (read, write, shell) or research (read-only "
                            "files and web). Default worker; only research in plan mode."
                        ),
                    },
                    "worktree": {
                        "type": "boolean",
                        "description": (
                            "Run the child in its own git worktree (TD-5126). "
                            "Default false: it works in your files."
                        ),
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


def worker_child_registry(
    parent: ToolRegistry, kind: DelegateKind = "worker", *, consult: bool = False
) -> ToolRegistry:
    """Copy *kind*'s tools from *parent*. Never includes ``delegate``.

    *consult* adds ``consult`` (TD-5119): config offers it to the worker
    tier, which every child runs on.
    """
    child = ToolRegistry()
    names = (*CHILD_TOOLS[kind], CONSULT_TOOL_NAME) if consult else CHILD_TOOLS[kind]
    for name in names:
        tool = parent.get(name)
        if tool is not None:
            child.register(tool)
    return child


async def handle_delegate(
    session: object,
    task: str,
    context: str = "",
    kind: str | None = None,
    worktree: bool | str = False,
    tool_call_id: str = "",
) -> HandlerOutput:
    """``delegate`` handler — run a one-level worker or research child."""
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
    router = session.router
    resolved = resolve_kind(kind, plan_mode=router is not None and router.plan_mode)
    steps: list[WorkerStep] = []
    own_tree = await open_child_tree(session, worktree)
    try:
        summary = await run_worker_child(
            parent=session,
            task=task,
            context=context,
            kind=resolved,
            transcript=steps,
            worktree=own_tree,
        )
    except HandlerRefusal as e:
        await close_child_tree(own_tree, task, steps)
        raise HandlerRefusal(e.code, e.message, tuple(steps)) from e
    except Exception as e:
        # A child that wrote files or ran commands before it failed still
        # shows them in the window (TD-5160).
        await close_child_tree(own_tree, task, steps)
        raise HandlerFailed(e, tuple(steps)) from e
    note = await close_child_tree(own_tree, task, steps)
    if note:
        summary = f"{summary}\n\n{note}"
    return HandlerOutput(text=summary, transcript=tuple(steps))


async def run_worker_child(
    *,
    parent: Session,
    task: str,
    context: str = "",
    kind: DelegateKind = "worker",
    transcript: list[WorkerStep] | None = None,
    worktree: SessionWorktree | None = None,
) -> str:
    """Slim worker loop: assemble, complete, dispatch, repeat until text or cap.

    Does not call ``agent_loop`` — no autonomy overlay, no CU episode, no
    second title-bar session.  Cost records land on the parent tracker
    with ``source="worker"``.  *kind* picks the child's tools (TD-5117);
    each step is appended to *transcript* for the window.
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
    from ..loop import _cap_violation, _tool_message_content, dispatch_worker_child_tools

    tracker = parent.cost_tracker
    if tracker is None:
        tracker = CostTracker(runtime.config)
        parent.cost_tracker = tracker

    parent_dispatcher = runtime.dispatcher
    child_registry = worker_child_registry(
        parent_dispatcher.registry, kind, consult=runtime.config.consult.allows("worker")
    )
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
    child_dispatcher.run_permissions_fn = parent_dispatcher.run_permissions_fn
    child_dispatcher.on_class_c = parent_dispatcher.on_class_c
    for tool in child_registry.list_tools():
        handler = parent_dispatcher._handlers.get(tool.name)
        if handler is not None:
            child_dispatcher.register_handler(tool.name, handler)

    # TD-5126: a child in its own worktree is told that root, not the parent's.
    assembler = runtime.assembler
    if worktree is not None:
        assembler = runtime.assembler.for_workspace(worktree.root)
    child = Session(parent.workspace_path if worktree is None else str(worktree.root))
    child.delegate_depth = 1
    child.parent_id = parent.id
    child.boundary_config = parent.boundary_config
    # The session's own config, not the library cache (TD-4843): web_search
    # and web_fetch read their endpoints from it, the registry's host
    # resolver already closes over the same search block, and the shell's
    # read-only list (TD-5103) comes from it too.
    child.config = parent.config
    child.policy = parent.policy
    child.trust_store = parent.trust_store
    child.config_view = parent.config_view
    child.cost_tracker = tracker
    child.touched_paths = parent.touched_paths
    if worktree is not None:
        # Root-bound pieces follow the child into its worktree (TD-5126).
        child.worktree = worktree
        bind_child_root(parent, child, child_dispatcher)
    child.autonomy = False
    # TD-5119: the child acts as the worker tier, and its consults count
    # against the parent's turn.
    child.acting_tier = "worker"
    child.consult_tally = parent.consult_tally
    # web_fetch's extract pass (TD-5114) reaches the worker tier and this
    # child's result cap through the runtime; without one a research
    # child's question was refused and it read the raw page instead.
    # delegate_depth still refuses a nested delegate.
    child.delegate_runtime = DelegateRuntime(
        provider_factory=runtime.provider_factory,
        config=runtime.config,
        dispatcher=child_dispatcher,
        assembler=assembler,
        session_start=runtime.session_start,
        # The parent's count, so a consult in the child checks the
        # iteration cap the parent is under.
        iterations=runtime.iterations,
        meter=runtime.meter or parent,
    )

    caps = parent.boundary_config.caps
    remaining = max(0, caps.max_iterations - runtime.iterations)
    child_limit = min(CHILD_MAX_ITERATIONS, remaining)
    last_text = ""
    worker_cfg = runtime.config.tier("worker")
    # The child's results go back to the worker tier: an image fs_read
    # opens is sent only if that tier takes images (TD-5130).
    max_images = runtime.config.computer_use.max_prompt_images
    child.tool_vision = worker_cfg.vision
    child.tool_max_images = max_images
    counter = make_token_counter(worker_cfg.slug)
    record = TranscriptRecorder(transcript)

    def finish(text: str) -> str:
        return cap_worker_summary(text, counter=counter)

    task_text = task.strip()
    if context.strip():
        task_text = f"{task_text}\n\nContext:\n{context.strip()}"
    record.add("task", task_text)
    tool_names = [t.name for t in child_registry.list_tools()]
    user_blob = f"{child_preamble(kind, tool_names)}\n\nTask:\n{task_text}"
    if worktree is not None:
        user_blob = f"{user_blob}\n\n{worktree_brief(parent.workspace_path, worktree)}"

    assembled = await assembler.assemble(
        "worker",
        task=user_blob,
        matched_paths=set(parent.touched_paths),
    )
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=assembled.text),
        ChatMessage(role="user", content=user_blob),
    ]

    provider = await _provider_for(runtime.provider_factory, worker_cfg)
    tool_defs = child_registry.to_provider_definitions()

    if child_limit == 0:
        record.add("note", "Worker stopped: parent iteration cap.")
        return finish("Worker stopped: parent iteration cap.")

    for child_i in range(child_limit):
        if parent.cancel_requested or parent.stop_turn_requested:
            why = "parent cancelled" if parent.cancel_requested else "parent stopped the turn"
            record.add("note", f"Worker stopped: {why}.")
            return finish(last_text or f"Worker stopped: {why}.")
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
            record.add("note", f"Worker stopped: {violation}")
            return finish(last_text or f"Worker stopped: {violation}")

        cap_prompt_images(messages, max_images)
        request = ChatCompletionRequest(
            model=worker_cfg.require_slug(),
            messages=list(messages),
            tools=tool_defs or None,
            stream=False,
            max_tokens=worker_cfg.max_output_tokens,
            extra_body=worker_cfg.extra_body,
        )
        resp = await provider.chat_completion(request)
        if isinstance(resp, ProviderError):
            record.add("note", f"Worker stopped: {resp.message}")
            return finish(last_text or f"Worker stopped: {resp.message}")
        if resp.usage is not None:
            tracker.record("worker", resp.usage, worker_cfg, source="worker")
            await parent.event_log.add(tracker.emit_cost_update(parent.id))

        tool_calls = resp.message.tool_calls or []
        content = resp.message.content or ""
        if content.strip():
            last_text = content
            record.add("reply", content)

        # Stopped at max_output_tokens (TD-5008): a cut call is not run,
        # and goes back to the worker with empty arguments and a note.
        cut: list[ToolCall] = []
        if resp.finish_reason == LENGTH_FINISH:
            tool_calls, cut = split_cut_calls(tool_calls)
            log_output_cap(
                session_id=parent.id,
                tier="worker",
                model=worker_cfg.require_slug(),
                max_output_tokens=worker_cfg.max_output_tokens,
                cut_calls=len(cut),
            )

        if not tool_calls and not cut:
            finished = content.strip() or last_text or "Worker finished with no summary."
            return finish(finished)

        if cut:
            record.add("note", f"{len(cut)} tool call(s) cut at the output cap and not run.")

        if cut:
            stubs = [stub_call(tc.id, tc.function.name if tc.function else "") for tc in cut]
            messages.append(replace(resp.message, tool_calls=[*tool_calls, *stubs]))
            messages.extend(cut_call_reply(tc.id, worker_cfg.max_output_tokens) for tc in cut)
        else:
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
            record.add("tool_call", raw or "{}", tool=name)

        results = await dispatch_worker_child_tools(child_dispatcher, child, items)
        for r in results:
            record.add("tool_result", r.output, tool=r.name, status=r.status)
            content = _tool_message_content(r) if r.images else r.output
            messages.append(ChatMessage(role="tool", content=content, tool_call_id=r.tool_call_id))

    record.add("note", "Worker stopped: iteration cap.")
    return finish(last_text or "Worker stopped: iteration cap.")


async def _provider_for(
    factory: Callable[..., Awaitable[Any]],
    tier_cfg: TierConfig,
) -> Any:
    """Call *factory* with the worker tier; zero-arg factories still work."""
    try:
        return await factory(tier_cfg)
    except TypeError:
        return await factory()
