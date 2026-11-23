"""``delegate`` in the background, and the tools that read and stop it (TD-5299).

``delegate`` with ``background: true`` starts the child as a task the
session owns and answers at once. A child that can write always gets its
own worktree, opened here, so the parent can keep editing its own files;
where there can be none the call is refused with a hint to run it in the
foreground. The worker belongs to its turn (``worker_jobs``).

``delegate_output`` reads a worker's progress and ``delegate_stop`` ends
one. Both touch only the daemon's record of this session's own workers.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import replace

from ..session import Session
from ..worker_jobs import WorkerJob
from .delegate import ChildTask, bound_runtime, run_child_task
from .delegate_child import WorkerLimits, resolve_kind
from .delegate_named import resolve_named_worker
from .delegate_worktree import close_child_tree, open_child_tree
from .registry import Tool, ToolRegistry
from .results import HandlerRefusal

DELEGATE_OUTPUT_TOOL = Tool(
    name="delegate_output",
    description=(
        "Read a background worker's progress: its state, calls, spend, last steps and, "
        "once it settled, its summary. Omit worker_id to list them all. You do not need "
        "to poll: a settled worker's result arrives as a note on its own."
    ),
    parameters={
        "type": "object",
        "properties": {
            "worker_id": {"type": "string", "description": "e.g. w-2. Omit for all."},
            "wait_secs": {"type": "integer", "minimum": 0, "maximum": 30},
        },
        "additionalProperties": False,
    },
    side_effect_class="auto",
    parallel_safe=True,
    mutates=False,
)

DELEGATE_STOP_TOOL = Tool(
    name="delegate_stop",
    description="Stop one background worker and get back what it had. Others keep running.",
    parameters={
        "type": "object",
        "properties": {"worker_id": {"type": "string"}},
        "required": ["worker_id"],
        "additionalProperties": False,
    },
    side_effect_class="auto",
    parallel_safe=False,
    mutates=False,
)

_STEPS_SHOWN = 4
_STEP_CHARS = 500


def register_delegate_job_tools(registry: ToolRegistry) -> None:
    registry.register(DELEGATE_OUTPUT_TOOL)
    registry.register(DELEGATE_STOP_TOOL)


def _unattended(parent: Session) -> bool:
    from .ask_user import denies_every_ask

    return parent.autonomy or parent.scheduled_run or denies_every_ask(parent)


async def spawn(parent: Session, spec: ChildTask) -> str:
    """Start *spec* in the background and say how to follow it."""
    _, runtime = bound_runtime(parent, "delegate")
    settings = runtime.config.delegate.background
    if _unattended(parent):
        raise HandlerRefusal(
            "delegate_background_unattended",
            "Refused: nobody is watching this run, so a background worker cannot be "
            "started. Run it in the foreground (background: false).",
        )
    jobs = parent.workers
    if jobs.spawned_this_turn >= settings.max_per_turn:
        raise HandlerRefusal(
            "delegate_background_limit",
            f"Refused: this turn already started {settings.max_per_turn} background "
            "workers (delegate.background.max_per_turn).",
        )
    router = parent.router
    plan_mode = router is not None and router.plan_mode
    named = None
    if spec.agent is not None:
        named = await resolve_named_worker(
            parent.workspace_path, spec.agent, kind=spec.kind, plan_mode=plan_mode
        )
    kind = resolve_kind(spec.kind, plan_mode=plan_mode)
    writes = named.writes if named is not None else kind == "worker"
    tree = None
    if writes:
        try:
            tree = await open_child_tree(parent, True, forced=True)
        except HandlerRefusal as e:
            raise HandlerRefusal(
                e.code,
                f"{e.message} A background worker that writes needs its own worktree; "
                "run this one in the foreground (background: false) instead.",
            ) from e
    job = jobs.new(
        call_id=spec.call_id,
        label=spec.label,
        kind=kind,
        task=spec.task,
        agent=named.name if named is not None else None,
    )
    if tree is not None:
        job.branch = tree.branch
        job.worktree_path = str(tree.path)
    tracker = parent.cost_tracker
    spent = tracker.session_cost() if tracker is not None else 0.0
    left = max(0.0, parent.boundary_config.caps.spend_usd - spent)
    limits = WorkerLimits(
        max_calls=settings.max_iterations,
        max_usd=min(settings.max_usd, left / settings.max_running),
        deadline=time.monotonic() + settings.max_minutes * 60,
    )

    async def progress(cost: float) -> None:
        job.calls += 1
        job.cost_usd += cost
        await jobs.changed(job, progress=True)

    ready = replace(
        spec, opened=tree, limits=limits, steps=job.steps, progress=progress, isolate_writer=True
    )
    job.handle = asyncio.create_task(_run(parent, job, ready, settings.max_running))
    await jobs.changed(job)
    where = (
        f" It works in its own worktree on branch {tree.branch}, from your last commit: "
        "your uncommitted edits are not in it."
        if tree is not None
        else " It reads your files as they are, even ones you are halfway through editing."
    )
    return (
        f"Started worker {job.id} ({job.label}).{where} Its result will arrive as a note. "
        "Read progress with delegate_output; stop it with delegate_stop."
    )


async def _run(parent: Session, job: WorkerJob, spec: ChildTask, max_running: int) -> None:
    """The worker's task: wait for a slot, run, settle. Never raises."""
    jobs = parent.workers
    started = False
    try:
        while jobs.running_count() >= max_running:
            await jobs.wait_change()
        job.state = "running"
        await jobs.changed(job)
        started = True
        done = await run_child_task(parent, spec)
        job.stopped = done.stopped
        if done.branch is None:
            job.branch = None
        await jobs.settle(job, "stopped" if done.stopped else "done", done.summary)
    except asyncio.CancelledError:
        # Stop, cancel or delegate_stop. Once started, run_child_task
        # settled the worktree; a queued worker's is settled here.
        if not started and spec.opened is not None:
            await asyncio.shield(close_child_tree(spec.opened, spec.task, job.steps))
        await jobs.settle(job, "stopped", _had(job))
    except HandlerRefusal as e:
        await jobs.settle(job, "failed", e.message)
    except Exception as e:
        if not started and spec.opened is not None:
            await close_child_tree(spec.opened, spec.task, job.steps)
        await jobs.settle(job, "failed", f"The worker failed: {e}")


def _had(job: WorkerJob) -> str:
    replies = [s.text for s in job.steps if s.kind == "reply"]
    why = job.stopped or "stopped"
    return replies[-1] if replies else f"Worker stopped: {why}."


def _describe(job: WorkerJob, *, steps: bool) -> str:
    head = (
        f"{job.id} ({job.label}): {job.state}"
        f"{f' ({job.stopped})' if job.stopped else ''}; "
        f"{job.calls} calls, ${job.cost_usd:.4f}"
        f"{f'; branch {job.branch}' if job.branch else ''}"
    )
    if not steps:
        return head
    lines = [head]
    for step in job.steps[-_STEPS_SHOWN:]:
        text = " ".join(step.text.split())[:_STEP_CHARS]
        lines.append(f"  {step.kind}{f' {step.tool}' if step.tool else ''}: {text}")
    if not job.live:
        lines.append(f"Summary:\n{job.summary}")
    return "\n".join(lines)


async def delegate_output(
    *, session: object, tool_call_id: str = "", worker_id: str | None = None, wait_secs: int = 0
) -> str:
    """One worker's progress, or the list. Waits up to *wait_secs* for a change."""
    if not isinstance(session, Session):
        raise HandlerRefusal("delegate_no_session", "Error: delegate_output requires a session")
    jobs = session.workers
    if worker_id is None:
        listed = [_describe(j, steps=False) for j in jobs.all()]
        return "\n".join(listed) if listed else "No background workers in this session."
    job = jobs.get(worker_id)
    if job is None:
        raise HandlerRefusal("worker_not_found", f"There is no worker {worker_id!r}.")
    if job.live and wait_secs > 0:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(jobs.wait_change(), timeout=min(wait_secs, 30))
    if not job.live:
        # Read here, so the loop does not hand the model the same result again.
        job.delivered = True
    return _describe(job, steps=True)


async def delegate_stop(*, session: object, tool_call_id: str = "", worker_id: str) -> str:
    """Stop one worker; its siblings run on."""
    if not isinstance(session, Session):
        raise HandlerRefusal("delegate_no_session", "Error: delegate_stop requires a session")
    jobs = session.workers
    job = jobs.get(worker_id)
    if job is None or not await jobs.stop(worker_id, "delegate_stop"):
        state = "" if job is None else f" It is already {job.state}."
        raise HandlerRefusal(
            "worker_not_found", f"There is no running worker {worker_id!r}.{state}"
        )
    job.delivered = True
    return _describe(job, steps=True)
