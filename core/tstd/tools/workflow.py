"""Several workers at once, each result checked (TD-5195, Ultracode).

``workflow`` is registered wherever ``delegate`` is, at depth 0, and stays
in the tool list whether Ultracode is on or off: the tool list and the
system prompt the provider caches never change when the switch flips.
The handler refuses while the switch is off.

Each task runs through :func:`run_child_task`, the path one ``delegate``
call takes. At most ``workflow.max_parallel`` run at once and results
come back in task order. With two or more tasks, every child that writes
works in its own worktree, so no two of them edit the same files.

With ``check`` on, a task that finishes is handed, still in its slot, to
a read-only reviewer on the validator tier (the worker tier when the
validator has no model). It replies with a verdict: confirmed, doubtful
or wrong. A reply with no verdict counts as doubtful; a failed task gets
no check.

It is still one level (spec §8): no child can delegate or run a
workflow, children do not see each other, and there is no second window.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any, Literal, get_args

from ..config import ModelConfig, TierName
from ..logging import get_logger
from ..protocol import WorkerStep
from ..provider import MessageContent
from ..session import Session
from .delegate import ChildTask, bound_runtime, run_child_task
from .delegate_child import DELEGATE_KINDS, STEP_CHAR_CAP, TRANSCRIPT_CHAR_BUDGET
from .delegate_named import AGENT_ARG
from .dispatch import ToolDispatcher
from .registry import Tool, ToolRegistry
from .results import HandlerFailed, HandlerOutput, HandlerRefusal

log = get_logger("tstd.workflow")

WORKFLOW_TOOL = "workflow"

Verdict = Literal["confirmed", "doubtful", "wrong"]
VERDICTS: tuple[Verdict, ...] = get_args(Verdict)
_BY_WORD: dict[str, Verdict] = {v: v for v in VERDICTS}

#: What the model is told on each turn's message while Ultracode is on.
ULTRACODE_NOTE = (
    "[Ultracode is on. For a substantial task, plan it, run the independent pieces at "
    "the same time with the workflow tool, let its checks review each result, then "
    "answer from the checked results. Answer small questions and one-line fixes directly.]"
)

_LABEL_CHARS = 60
_REASON_LINES = 5
_REASON_CHARS = 300
# Markdown around either half is common: "**VERDICT**: wrong", "Verdict: `wrong`".
_VERDICT_LINE = re.compile(
    r"VERDICT\s*[*_]*\s*:\s*[*_`]*\s*(confirmed|doubtful|wrong)\b(?!\s*[|/])", re.IGNORECASE
)

_DESCRIPTION = (
    "Ultracode only. Runs several workers at once on independent pieces of the job, "
    "at this session's effort, and returns each one's summary. With check on (the "
    "default), a separate reviewer checks each result before it comes back. Workers "
    "do not see this conversation or each other, so give each task everything it "
    "needs. Writing workers each get their own worktree branch."
)


def register_workflow_tool(registry: ToolRegistry) -> None:
    """Register ``workflow`` beside ``delegate``. Call from ``create_registry``."""
    registry.register(
        Tool(
            name=WORKFLOW_TOOL,
            description=_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "task": {
                                    "type": "string",
                                    "description": "Everything this worker needs for its piece",
                                },
                                "kind": {
                                    "type": "string",
                                    "enum": list(DELEGATE_KINDS),
                                    "description": (
                                        "worker (read, write, shell; the default) or "
                                        "research (read-only files and web)"
                                    ),
                                },
                                AGENT_ARG: {
                                    "type": "string",
                                    "description": "A named worker to run instead of a kind",
                                },
                                "label": {
                                    "type": "string",
                                    "description": "A short name shown in the results",
                                },
                            },
                            "required": ["task"],
                        },
                    },
                    "check": {
                        "type": "boolean",
                        "description": "Review each result before it comes back. Default true.",
                    },
                },
                "required": ["tasks"],
            },
            side_effect_class="ask",
            # It runs its own concurrency, bounded by workflow.max_parallel.
            parallel_safe=False,
            path_fields=(),
            host_fields=(),
        )
    )


def register_workflow_handler(dispatcher: ToolDispatcher) -> None:
    """Register the ``workflow`` handler. Call from ``register_builtin_handlers``."""
    if dispatcher.registry.get(WORKFLOW_TOOL) is None:
        raise KeyError("workflow tool must be registered before its handler")
    dispatcher.register_handler(WORKFLOW_TOOL, handle_workflow)


def with_ultracode_note(content: MessageContent) -> MessageContent:
    """*content* with the Ultracode note after it, as the model receives it.

    On the turn's user message, never the system prompt (TD-5194 freezes
    it per turn), so the provider's prompt cache survives the switch.
    """
    if isinstance(content, str):
        return f"{content}\n\n{ULTRACODE_NOTE}"
    return [*content, {"type": "text", "text": ULTRACODE_NOTE}]


@dataclass(frozen=True)
class _Plan:
    number: int
    task: str
    kind: str | None
    agent: str | None
    label: str


@dataclass(frozen=True)
class _Outcome:
    summary: str
    steps: tuple[WorkerStep, ...]
    # Why the task did not finish; ``None`` when it did.
    failed: str | None = None
    branch: str | None = None
    diff: str | None = None
    verdict: Verdict | None = None
    reasons: str = ""
    check_steps: tuple[WorkerStep, ...] = ()


async def handle_workflow(
    session: object,
    tasks: list[Any],
    check: bool = True,
    tool_call_id: str = "",
) -> HandlerOutput:
    """``workflow`` handler: run the tasks side by side, check each, report in order."""
    parent, runtime = bound_runtime(session, WORKFLOW_TOOL)
    router = parent.router
    if router is None or not router.ultracode:
        raise HandlerRefusal(
            "workflow_off",
            "Refused: Ultracode is off. Turn on Ultracode in the model picker, or run "
            "one worker with delegate.",
        )
    limits = runtime.config.workflow
    if not tasks:
        raise HandlerRefusal("workflow_empty", "Error: workflow needs at least one task.")
    if len(tasks) > limits.max_tasks:
        raise HandlerRefusal(
            "workflow_too_many",
            f"Refused: {len(tasks)} tasks, and one workflow runs at most {limits.max_tasks} "
            "(workflow.max_tasks). Merge some, or run the rest in a second call.",
        )
    plans = [_plan(i + 1, raw) for i, raw in enumerate(tasks)]
    base = tool_call_id or uuid.uuid4().hex[:12]
    runs = len(plans) * (2 if check else 1)
    budget = max(STEP_CHAR_CAP, TRANSCRIPT_CHAR_BUDGET // runs)
    checker = _checker_tier(runtime.config)
    slots = asyncio.Semaphore(limits.max_parallel)

    async def one(plan: _Plan) -> _Outcome:
        async with slots:
            spec = ChildTask(
                task=plan.task,
                kind=plan.kind,
                agent=plan.agent,
                isolate_writer=len(plans) >= 2,
                call_id=f"{base}.{plan.number}",
                transcript_budget=budget,
            )
            outcome = await _run(parent, spec)
            if not check or outcome.failed is not None:
                return outcome
            return await _check(
                parent, plan, outcome, checker, f"{base}.{plan.number}.check", budget
            )

    # Every task finishes, and settles its worktree, before a cancellation
    # (Stop, a cancelled approval) ends the call: a plain gather would raise
    # at the first one and leave its siblings running with nobody waiting.
    settled = await asyncio.gather(*(one(plan) for plan in plans), return_exceptions=True)
    outcomes: list[_Outcome] = []
    for result in settled:
        if isinstance(result, BaseException):
            raise result
        outcomes.append(result)
    return HandlerOutput(
        text=_report(plans, outcomes, check), transcript=_transcript(plans, outcomes)
    )


def _plan(number: int, raw: object) -> _Plan:
    """One task item, checked; a malformed one refuses the whole call."""
    if not isinstance(raw, dict):
        raise HandlerRefusal("workflow_task", f"Error: task {number} is not an object.")
    task = raw.get("task")
    if not isinstance(task, str) or not task.strip():
        raise HandlerRefusal("workflow_task", f"Error: task {number} has no task text.")
    kind = raw.get("kind")
    if kind is not None and kind not in DELEGATE_KINDS:
        raise HandlerRefusal("workflow_task", f"Error: task {number} has unknown kind {kind!r}.")
    agent = raw.get(AGENT_ARG)
    if agent is not None and not isinstance(agent, str):
        raise HandlerRefusal("workflow_task", f"Error: task {number}'s agent is not a name.")
    label = raw.get("label")
    shown = label.strip() if isinstance(label, str) and label.strip() else ""
    return _Plan(
        number=number,
        task=task,
        kind=kind,
        agent=agent,
        label=shown or " ".join(task.split())[:_LABEL_CHARS],
    )


def _aborted(parent: Session) -> bool:
    return parent.cancel_requested or parent.stop_turn_requested


async def _run(parent: Session, spec: ChildTask) -> _Outcome:
    """One task as a ``delegate`` call would run it, its failure kept as data."""
    if _aborted(parent):
        return _Outcome("", (), failed="the turn was stopped before it started")
    try:
        done = await run_child_task(parent, spec)
    except HandlerRefusal as e:
        return _Outcome("", e.transcript, failed=_reason(e.message))
    except HandlerFailed as e:
        return _Outcome("", e.transcript, failed=_reason(str(e)))
    except Exception as e:  # one task's crash must not take its siblings down
        log.exception("workflow task failed", extra={"extra_fields": {"session_id": parent.id}})
        return _Outcome("", (), failed=_reason(str(e) or type(e).__name__))
    return _Outcome(
        done.summary, done.steps, failed=done.stopped, branch=done.branch, diff=done.diff
    )


async def _check(
    parent: Session,
    plan: _Plan,
    outcome: _Outcome,
    tier: TierName,
    call_id: str,
    budget: int,
) -> _Outcome:
    """*outcome* with a reviewer's verdict on it."""
    task, context = _check_brief(plan, outcome)
    spec = ChildTask(
        task=task,
        context=context,
        kind="research",
        call_id=call_id,
        tier=tier,
        source="check",
        transcript_budget=budget,
    )
    checked = await _run(parent, spec)
    if checked.failed is not None:
        return replace(
            outcome,
            verdict="doubtful",
            reasons=f"The check did not finish: {checked.failed}.",
            check_steps=checked.steps,
        )
    verdict, reasons = parse_verdict(checked.summary)
    return replace(outcome, verdict=verdict, reasons=reasons, check_steps=checked.steps)


def _check_brief(plan: _Plan, outcome: _Outcome) -> tuple[str, str]:
    """The reviewer's task and context: the worker's task, its summary, its diff."""
    against = "the files it names and the diff below" if outcome.diff else "the files it names"
    task = (
        "Check another worker's result. Do not redo its task and do not change anything: "
        f"test each claim in its summary against {against}. Your first line must be "
        '"VERDICT: " followed by one word, confirmed, doubtful or wrong; then give at '
        f"most {_REASON_LINES} short lines of reasons."
    )
    context = f"The worker's task:\n{plan.task.strip()}\n\nThe worker's summary:\n{outcome.summary}"
    if outcome.diff:
        context += (
            f"\n\nIts changes are committed on branch {outcome.branch}, not in the "
            f"workspace files. The diff:\n```diff\n{outcome.diff}\n```"
        )
    return task, context


def parse_verdict(reply: str) -> tuple[Verdict, str]:
    """The reviewer's verdict and up to five lines of reasons; no verdict is doubtful."""
    lines = reply.splitlines()
    for i, line in enumerate(lines):
        match = _VERDICT_LINE.search(line)
        if match is None:
            continue
        verdict = _BY_WORD[match.group(1).lower()]
        rest = line[match.end() :].strip(" .:-—*")
        reasons = [rest] if rest else []
        reasons += [r.strip() for r in lines[i + 1 :] if r.strip()]
        return verdict, "\n".join(r[:_REASON_CHARS] for r in reasons[:_REASON_LINES])
    first = next((line.strip() for line in lines if line.strip()), "")
    said = f" It said: {first[:200]}" if first else ""
    return "doubtful", f"The checker gave no verdict.{said}"


def _checker_tier(config: ModelConfig) -> TierName:
    """The validator, or the worker tier when the validator has no model."""
    return "validator" if config.tier("validator").slug is not None else "worker"


def _reason(message: str) -> str:
    """A refusal or error as a short reason: no ``Refused:`` prefix, one line."""
    text = message.strip().removeprefix("Refused: ").removeprefix("Error: ")
    first = text.splitlines()[0] if text else "unknown error"
    return first.rstrip(".")[:200]


def _report(plans: list[_Plan], outcomes: list[_Outcome], check: bool) -> str:
    """What the parent model gets: a head line, then each task in order."""
    done = sum(o.failed is None for o in outcomes)
    failed = len(outcomes) - done
    noun = "task" if len(plans) == 1 else "tasks"
    head = f"Workflow: {len(plans)} {noun}, {done} done"
    head += f", {failed} failed." if failed else "."
    if check:
        counts = Counter(o.verdict for o in outcomes if o.verdict is not None)
        parts = [f"{counts[v]} {v}" for v in VERDICTS if counts[v]]
        head += f" Checks: {', '.join(parts)}." if parts else " Checks: none ran."
    else:
        head += " Checks: off."
    blocks = [head]
    for plan, outcome in zip(plans, outcomes, strict=True):
        status = "done" if outcome.failed is None else f"failed: {outcome.failed}"
        title = f"## {plan.number}. {plan.label}  [{status}]"
        if outcome.verdict is not None:
            title += f" [check: {outcome.verdict}]"
        body = [title]
        if outcome.summary:
            body.append(outcome.summary)
        if outcome.verdict is not None and outcome.reasons:
            body.append(f"Check: {outcome.reasons}")
        blocks.append("\n".join(body))
    return "\n\n".join(blocks)


def _transcript(plans: list[_Plan], outcomes: list[_Outcome]) -> tuple[WorkerStep, ...]:
    """Every worker's and checker's steps, each under a header, for the window."""
    steps: list[WorkerStep] = []
    for plan, outcome in zip(plans, outcomes, strict=True):
        steps.append(WorkerStep(kind="note", text=f"Task {plan.number}: {plan.label}"))
        steps.extend(outcome.steps)
        if outcome.check_steps:
            steps.append(WorkerStep(kind="note", text=f"Check of task {plan.number}"))
            steps.extend(outcome.check_steps)
    return tuple(steps)
