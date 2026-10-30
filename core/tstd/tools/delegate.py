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
``agent`` runs a named worker instead (TD-5133): its own prompt, tool
allow list, tier and tightened policy, from ``.tst/agents/*.md`` or
``~/.tstdesk/agents/*.md``.

The ``workflow`` tool (TD-5195) runs several children at once through
:func:`run_child_task`, the same path as one ``delegate`` call. So that
children can run side by side, each child's tool call ids carry its
parent call's id, Stop reaches its model call and its tools, and every
child's model calls count on one iteration counter.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ..config import ModelConfig, TierConfig, TierName
from ..context import PromptAssembler
from ..context.tokens import make_token_counter
from ..effort import with_effort
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
from .consult import _unless_stopped as unless_stopped
from .delegate_child import (
    CHILD_TOOLS,
    CONSULT_TOOL_NAME,
    DELEGATE_KINDS,
    RESEARCH_CHILD_TOOLS,
    SUMMARY_TOKEN_CAP,
    TRANSCRIPT_CHAR_BUDGET,
    WORKER_CHILD_TOOLS,
    DelegateKind,
    TranscriptRecorder,
    cap_worker_summary,
    child_preamble,
    resolve_kind,
)
from .delegate_named import (
    AGENT_ARG,
    named_preamble,
    named_system_prompt,
    named_tool_names,
    resolve_named_worker,
    worker_tier,
)
from .delegate_worktree import (
    bind_child_root,
    branch_diff,
    close_child_tree,
    open_child_tree,
    worktree_brief,
)
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
    "ChildDone",
    "ChildTask",
    "DelegateRuntime",
    "bound_runtime",
    "cap_worker_summary",
    "handle_delegate",
    "register_delegate_handlers",
    "register_delegate_tools",
    "run_child_task",
    "run_worker_child",
    "worker_child_registry",
]

if TYPE_CHECKING:  # TD-5161: context.workers imports tools; no cycle at import
    from ..context.workers import WorkerDefinition
    from ..cost import CostTracker

log = get_logger("tstd.delegate")

CHILD_MAX_ITERATIONS = 8


@dataclass
class DelegateRuntime:
    """Parent-loop host bound so ``delegate`` can spawn a worker child.

    ``web_fetch``'s extract pass reaches the worker tier and the result
    cap through it too (TD-5114).  ``iterations`` is mutated by the parent
    loop after each parent model call; ``child_calls`` counts every model
    call this session's children make (TD-5195). A child checks the two
    together against ``max_iterations`` before each call, so children side
    by side share what is left instead of each spending all of it.
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
    child_calls: int = 0


@dataclass(frozen=True)
class ChildTask:
    """One child to run: a ``delegate`` call's arguments, or one workflow task."""

    task: str
    context: str = ""
    kind: str | None = None
    agent: str | None = None
    worktree: bool | str = False
    # TD-5195: a child that writes gets its own worktree whatever
    # ``worktree`` says, so writers side by side never share files.
    isolate_writer: bool = False
    # Prefix for the child's tool call ids: the parent's call id, and the
    # task's number in a workflow.
    call_id: str = ""
    # A tier other than the worker's (a workflow's checker), and the tag
    # its cost rows carry.
    tier: TierName | None = None
    source: str = "worker"
    transcript_budget: int = TRANSCRIPT_CHAR_BUDGET


@dataclass(frozen=True)
class ChildDone:
    """How a child run ended (TD-5195)."""

    summary: str
    steps: tuple[WorkerStep, ...]
    # Why it stopped before it finished (Stop, a cap, a provider error,
    # its own call limit); ``None`` when it answered.
    stopped: str | None = None
    # The branch its changes are committed on, and their diff, when it
    # worked in its own worktree and changed something.
    branch: str | None = None
    diff: str | None = None


@dataclass(frozen=True)
class _ChildEnd:
    summary: str
    stopped: str | None = None


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
                "its changes come back on a branch for you to merge. "
                "agent runs a named worker the user defined instead of a kind."
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
                    AGENT_ARG: {
                        "type": "string",
                        "description": (
                            "A named worker to run (TD-5133). It brings its own "
                            "prompt, tools and model; do not also give kind."
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
    parent: ToolRegistry,
    kind: DelegateKind = "worker",
    *,
    consult: bool = False,
    names: tuple[str, ...] | None = None,
) -> ToolRegistry:
    """Copy *kind*'s tools from *parent*. Never includes ``delegate``.

    *consult* adds ``consult`` (TD-5119): config offers it to the worker
    tier, which every child runs on. *names*, a named worker's allow list
    (TD-5133), replaces the kind's tools.
    """
    child = ToolRegistry()
    if names is None:
        names = (*CHILD_TOOLS[kind], CONSULT_TOOL_NAME) if consult else CHILD_TOOLS[kind]
    for name in names:
        if name == "delegate":
            continue
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
    agent: str | None = None,
    tool_call_id: str = "",
) -> HandlerOutput:
    """``delegate`` handler — run a one-level worker, research or named child."""
    parent, _runtime = bound_runtime(session, "delegate")
    done = await run_child_task(
        parent,
        ChildTask(
            task=task,
            context=context,
            kind=kind,
            agent=agent,
            worktree=worktree,
            call_id=tool_call_id,
        ),
    )
    return HandlerOutput(text=done.summary, transcript=done.steps)


def bound_runtime(session: object, tool: str) -> tuple[Session, DelegateRuntime]:
    """*session* and its runtime, or *tool*'s refusal: no session, nested, unbound."""
    if not isinstance(session, Session):
        raise HandlerRefusal(f"{tool}_no_session", f"Error: {tool} requires a session")
    if session.delegate_depth >= 1:
        raise HandlerRefusal(
            f"{tool}_nested",
            f"Refused: {tool} cannot nest (no grandchildren).",
        )
    runtime = session.delegate_runtime
    if not isinstance(runtime, DelegateRuntime):
        raise HandlerRefusal(
            f"{tool}_unbound",
            f"Error: {tool} runtime is not bound on this session",
        )
    return session, runtime


async def run_child_task(parent: Session, spec: ChildTask) -> ChildDone:
    """Run one child the way a ``delegate`` call does (TD-5195: ``workflow`` too).

    Resolves the named worker and kind, opens the child's worktree when
    it gets one, runs it and settles the worktree. A refusal or crash is
    raised with what the child did before it, for the window.
    """
    _, runtime = bound_runtime(parent, "delegate")
    router = parent.router
    plan_mode = router is not None and router.plan_mode
    named: WorkerDefinition | None = None
    if spec.agent is not None:
        named = await resolve_named_worker(
            parent.workspace_path, spec.agent, kind=spec.kind, plan_mode=plan_mode
        )
        worker_tier(runtime.config, named)
    resolved = resolve_kind(spec.kind, plan_mode=plan_mode)
    writes = named.writes if named is not None else resolved == "worker"
    steps: list[WorkerStep] = []
    own_tree = await open_child_tree(parent, spec.worktree, forced=spec.isolate_writer and writes)
    try:
        end = await _child_loop(
            parent=parent,
            task=spec.task,
            context=spec.context,
            kind=resolved,
            transcript=steps,
            worktree=own_tree,
            named=named,
            call_id=spec.call_id,
            tier=spec.tier,
            source=spec.source,
            transcript_budget=spec.transcript_budget,
        )
    except HandlerRefusal as e:
        await close_child_tree(own_tree, spec.task, steps)
        raise HandlerRefusal(e.code, e.message, tuple(steps)) from e
    except asyncio.CancelledError:
        # Stop, or an approval card cancelled under it: the worktree is
        # still settled, so no folder or branch is left behind (TD-5195).
        with contextlib.suppress(Exception):
            await asyncio.shield(close_child_tree(own_tree, spec.task, steps))
        raise
    except Exception as e:
        # A child that wrote files or ran commands before it failed still
        # shows them in the window (TD-5160).
        await close_child_tree(own_tree, spec.task, steps)
        raise HandlerFailed(e, tuple(steps)) from e
    note = await close_child_tree(own_tree, spec.task, steps)
    summary = f"{end.summary}\n\n{note}" if note else end.summary
    diff = await branch_diff(own_tree) if own_tree is not None else None
    return ChildDone(
        summary=summary,
        steps=tuple(steps),
        stopped=end.stopped,
        branch=own_tree.branch if own_tree is not None and diff is not None else None,
        diff=diff,
    )


async def run_worker_child(
    *,
    parent: Session,
    task: str,
    context: str = "",
    kind: DelegateKind = "worker",
    transcript: list[WorkerStep] | None = None,
    worktree: SessionWorktree | None = None,
    named: WorkerDefinition | None = None,
    call_id: str = "",
) -> str:
    """Slim worker loop: assemble, complete, dispatch, repeat until text or cap.

    Does not call ``agent_loop`` — no autonomy overlay, no CU episode, no
    second title-bar session.  Cost records land on the parent tracker
    with ``source="worker"``.  *kind* picks the child's tools (TD-5117);
    each step is appended to *transcript* for the window. *named* runs a
    named worker (TD-5133): its tools, tier, prompt and policy rules.
    Returns the capped summary.
    """
    end = await _child_loop(
        parent=parent,
        task=task,
        context=context,
        kind=kind,
        transcript=transcript,
        worktree=worktree,
        named=named,
        call_id=call_id,
    )
    return end.summary


async def _child_loop(
    *,
    parent: Session,
    task: str,
    context: str = "",
    kind: DelegateKind = "worker",
    transcript: list[WorkerStep] | None = None,
    worktree: SessionWorktree | None = None,
    named: WorkerDefinition | None = None,
    call_id: str = "",
    tier: TierName | None = None,
    source: str = "worker",
    transcript_budget: int = TRANSCRIPT_CHAR_BUDGET,
) -> _ChildEnd:
    """:func:`run_worker_child`'s loop, also saying why it stopped early."""
    parent, runtime = bound_runtime(parent, "delegate")

    from ..cost import CostTracker
    from ..loop import _tool_message_content, dispatch_worker_child_tools

    tracker = parent.cost_tracker
    if tracker is None:
        tracker = CostTracker(runtime.config)
        parent.cost_tracker = tracker

    parent_dispatcher = runtime.dispatcher
    acting: TierName = tier or "worker"
    worker_cfg = runtime.config.tier(acting)
    allow_list: tuple[str, ...] | None = None
    if named is not None:
        acting, worker_cfg = worker_tier(runtime.config, named)
        allow_list = named_tool_names(named, consult=runtime.config.consult.allows(acting))
    child_registry = worker_child_registry(
        parent_dispatcher.registry,
        kind,
        consult=runtime.config.consult.allows(acting),
        names=allow_list,
    )
    child_dispatcher = _child_dispatcher(parent, parent_dispatcher, child_registry, named)

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
    # TD-5134: the parent's hooks, whose runs land on the parent's audited log.
    child.hooks = parent.hooks
    child.cost_tracker = tracker
    child.touched_paths = parent.touched_paths
    # TD-5195: Stop reaches the child's own tools. Its shell and web_search
    # watch the child session, which shares the parent's abort events. A
    # child is never registered, so nothing stops or cancels it by itself.
    child._cancel_event = parent._cancel_event
    child._stop_turn_event = parent._stop_turn_event
    if worktree is not None:
        # Root-bound pieces follow the child into its worktree (TD-5126).
        child.worktree = worktree
        bind_child_root(parent, child, child_dispatcher)
        # TD-5161: the shared hooks run in the child's worktree, not the parent's.
        child.hooks = parent.hooks.in_folder(child.workspace_path)
    child.autonomy = False
    # TD-5119: the child acts as its tier, and its consults count against
    # the parent's turn.
    child.acting_tier = acting
    child.consult_tally = parent.consult_tally
    # web_fetch's extract pass (TD-5114) reaches the worker tier and this
    # child's result cap through the runtime; without one a research
    # child's question was refused and it read the raw page instead.
    # delegate_depth still refuses a nested delegate.
    child_runtime = DelegateRuntime(
        provider_factory=runtime.provider_factory,
        config=runtime.config,
        dispatcher=child_dispatcher,
        assembler=assembler,
        session_start=runtime.session_start,
        # The parent's count, so a consult in the child checks the
        # iteration cap the parent is under.
        iterations=runtime.iterations + runtime.child_calls,
        meter=runtime.meter or parent,
    )
    child.delegate_runtime = child_runtime

    last_text = ""
    # The child's results go back to its tier: an image fs_read opens is
    # sent only if that tier takes images (TD-5130).
    max_images = runtime.config.computer_use.max_prompt_images
    child.tool_vision = worker_cfg.vision
    child.tool_max_images = max_images
    counter = make_token_counter(worker_cfg.slug)
    record = TranscriptRecorder(transcript, transcript_budget)

    def finish(text: str, stopped: str | None = None) -> _ChildEnd:
        return _ChildEnd(cap_worker_summary(text, counter=counter), stopped)

    def stop(why: str) -> _ChildEnd:
        line = f"Worker stopped: {why}."
        record.add("note", line)
        return finish(last_text or line, why)

    task_text = task.strip()
    if context.strip():
        task_text = f"{task_text}\n\nContext:\n{context.strip()}"
    record.add("task", task_text)
    tool_names = [t.name for t in child_registry.list_tools()]
    preamble = (
        child_preamble(kind, tool_names) if named is None else named_preamble(named, tool_names)
    )
    user_blob = f"{preamble}\n\nTask:\n{task_text}"
    if worktree is not None:
        user_blob = f"{user_blob}\n\n{worktree_brief(parent.workspace_path, worktree)}"

    assembled = await assembler.assemble(
        "worker",
        task=user_blob,
        matched_paths=set(parent.touched_paths),
    )
    system = assembled.text if named is None else named_system_prompt(assembled.text, named)
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=user_blob),
    ]

    provider = await _provider_for(runtime.provider_factory, worker_cfg)
    tool_defs = child_registry.to_provider_definitions()
    # TD-5195: children run at the session's effort when their tier takes it.
    router = parent.router
    extra_body = worker_cfg.extra_body
    if router is not None:
        extra_body = with_effort(
            extra_body, router.effort, tier_supports=worker_cfg.reasoning_effort
        )
    # TD-5195: two children on one model both say ``call_0``; the parent's
    # call id in front keeps their approvals apart.
    prefix = call_id or uuid.uuid4().hex[:12]

    for _ in range(CHILD_MAX_ITERATIONS):
        if parent.cancel_requested or parent.stop_turn_requested:
            return stop(
                "parent cancelled" if parent.cancel_requested else "parent stopped the turn"
            )
        # The count is every child's calls, not this child's alone (TD-5195).
        used = runtime.iterations + runtime.child_calls
        violation = _child_cap_violation(
            parent, tracker, runtime.session_start, used, _skip_all(parent_dispatcher)
        )
        if violation is not None:
            return stop(violation)
        runtime.child_calls += 1
        child_runtime.iterations = used + 1

        cap_prompt_images(messages, max_images)
        request = ChatCompletionRequest(
            model=worker_cfg.require_slug(),
            messages=list(messages),
            tools=tool_defs or None,
            stream=False,
            max_tokens=worker_cfg.max_output_tokens,
            extra_body=extra_body,
        )
        resp = await unless_stopped(provider.chat_completion(request), [parent])
        if resp is None:
            return stop(
                "parent cancelled" if parent.cancel_requested else "parent stopped the turn"
            )
        if isinstance(resp, ProviderError):
            return stop(resp.message)
        if resp.usage is not None:
            tracker.record(acting, resp.usage, worker_cfg, source=source)
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
                tier=acting,
                model=worker_cfg.require_slug(),
                max_output_tokens=worker_cfg.max_output_tokens,
                cut_calls=len(cut),
            )

        if not tool_calls and not cut:
            return finish(content.strip() or last_text or "Worker finished with no summary.")

        if cut:
            record.add("note", f"{len(cut)} tool call(s) cut at the output cap and not run.")
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
            items.append((f"{prefix}/{tc.id}", name, parsed))
            record.add("tool_call", raw or "{}", tool=name)

        results = await dispatch_worker_child_tools(child_dispatcher, child, items)
        # Results come back in call order; the child's model is answered
        # under its own ids.
        for tc, r in zip(tool_calls, results, strict=True):
            record.add("tool_result", r.output, tool=r.name, status=r.status)
            content = _tool_message_content(r) if r.images else r.output
            messages.append(ChatMessage(role="tool", content=content, tool_call_id=tc.id))

    return stop("iteration cap")


def _skip_all(dispatcher: ToolDispatcher) -> bool:
    """Whether the person turned approvals off, as the parent loop reads it."""
    return bool(dispatcher.skip_all_fn is not None and dispatcher.skip_all_fn())


def _child_cap_violation(
    parent: Session,
    tracker: CostTracker,
    session_start: float,
    used: int,
    skip_all: bool,
) -> str | None:
    """The cap a child's next call would break, or ``None``.

    Spend is always checked: a worker must not keep billing past the
    parent's spend cap, skip-all or not. The iteration and wall-clock caps
    follow the parent loop: skip-all lifts them for an interactive session
    (TD-806). Before TD-5195 a child checked them even then, so on a long
    skip-all session every worker stopped once the session had made
    ``max_iterations`` calls since the daemon started.
    """
    from ..loop import _cap_violation

    if skip_all and not parent.autonomy:
        spend_cap = parent.boundary_config.caps.spend_usd
        if tracker.session_cost() >= spend_cap:
            return f"spend cap exceeded: ${tracker.session_cost():.4f} >= ${spend_cap:.2f}"
        return None
    return _cap_violation(parent, tracker, session_start, used, skip_all=False)


def _child_dispatcher(
    parent: Session,
    parent_dispatcher: ToolDispatcher,
    child_registry: ToolRegistry,
    named: WorkerDefinition | None,
) -> ToolDispatcher:
    """The child's dispatcher: the parent's gate, path guard and approvals."""
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
    # A named worker's rules sit beside the parent's (TD-5133); they can
    # only tighten, and a generic child has none.
    child_dispatcher.worker_rules = (
        (*parent_dispatcher.worker_rules, *named.policy)
        if named is not None
        else parent_dispatcher.worker_rules
    )
    for tool in child_registry.list_tools():
        handler = parent_dispatcher._handlers.get(tool.name)
        if handler is not None:
            child_dispatcher.register_handler(tool.name, handler)
    return child_dispatcher


async def _provider_for(
    factory: Callable[..., Awaitable[Any]],
    tier_cfg: TierConfig,
) -> Any:
    """Call *factory* with the worker tier; zero-arg factories still work."""
    try:
        return await factory(tier_cfg)
    except TypeError:
        return await factory()
