"""Tool dispatch — parse, validate, and execute tool calls.

The dispatcher resolves tool names against the registry, validates
arguments against each tool's JSON Schema, executes handlers, and
returns structured results.  Parallel-safe tools run concurrently.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any, Literal, cast

from jsonschema import ValidationError as SchemaError
from jsonschema import validate as validate_schema

from ..autonomy import (
    AmbiguousClassifier,
    Checkpointer,
    DecisionClass,
    DecisionLedger,
    DecisionRequest,
    LedgerEntry,
)
from ..desktop.protocol import DesktopError
from ..logging import get_logger
from ..memory_commit import MemoryCommitter
from ..policy import ApprovalOutcome, PolicyConfig, format_summary, resolve_explained
from ..protocol import DecisionLogged as DecisionLoggedEvent
from .boundary import PathGuard, RefusalError
from .diff import render_diff, snapshot_text
from .registry import Tool, ToolRegistry
from .results import HandlerRefusal, ToolResult, ValidationError, truncate_output

__all__ = [
    "ToolDispatcher",
    "ToolResult",
    "UnclassifiedToolCall",
    "ValidationError",
    "build_decision_request",
    "truncate_output",
]

log = get_logger("tstd.dispatch")


class UnclassifiedToolCall(Exception):
    """A tool reached execution without a decision classifier attached.

    Raising here is the chokepoint guarantee (prime directive §2.6): no
    tool executes unless the decision classifier has been run over the
    call.  Reaching the handler unclassified is a bypass, not a state the
    engine falls into by default.  Also raised when a call resolves to
    ``ask`` with no approval handler attached (TD-802) — the approval
    gate is part of the chokepoint, not an optional add-on.
    """


# Injected approval callback (TD-802).  Receives the tool call, its
# classification, and the human-readable summary/reason for the card;
# returns the outcome.  ``Session.request_approval`` is the production
# implementation; tests may inject an auto-approver.
ApprovalHandler = Callable[
    [str, Tool, dict[str, Any], DecisionClass, str, str],
    Awaitable[ApprovalOutcome],
]


def build_decision_request(tool: Tool, arguments: dict[str, Any]) -> DecisionRequest:
    """Reduce a tool call to the signals the decision classifier needs.

    Uses the tool's declared ``path_fields`` / ``host_fields`` /
    ``mutates`` / ``actuates`` metadata (TD-702, TD-3301) — never
    heuristics over raw argument text.  Read tools expose their
    ``path_fields`` as reads; mutating tools expose them as write
    targets.  Desktop CU tools keep those fields empty so PathGuard
    does not run; ``actuates`` is what the table classifies.
    """
    paths = tuple(Path(arguments[f]) for f in tool.path_fields if isinstance(arguments.get(f), str))
    hosts = frozenset(arguments[f] for f in tool.host_fields if isinstance(arguments.get(f), str))
    if tool.mutates:
        writes = paths
        reads: tuple[Path, ...] = ()
    else:
        writes = ()
        reads = paths
    return DecisionRequest(
        tool_name=tool.name,
        arguments=dict(arguments),
        writes=writes,
        reads=reads,
        hosts=hosts,
        is_mutation=tool.mutates,
        actuates=tool.actuates,
        source=tool.source,
    )


# ── Tool dispatcher ─────────────────────────────────────────────────────


class ToolDispatcher:
    """Dispatches tool calls from the model to registered handlers.

    Usage::

        dispatcher = ToolDispatcher(registry)
        dispatcher.register_handler("fs_read", my_read_handler)
        result = await dispatcher.dispatch("call_1", tool_call_args, session)
    """

    def __init__(
        self,
        registry: ToolRegistry,
        max_result_chars: int = 50_000,
        classifier: AmbiguousClassifier | None = None,
        path_guard: PathGuard | None = None,
        checkpointer: Checkpointer | None = None,
        memory_committer: MemoryCommitter | None = None,
        ledger: DecisionLedger | None = None,
        policy: PolicyConfig | None = None,
        approval_handler: ApprovalHandler | None = None,
        workspace: Path | None = None,
    ) -> None:
        self.registry = registry
        self.max_result_chars = max_result_chars
        # The decision classifier (TD-702).  A tool call reaching the
        # handler without a classifier attached raises ``UnclassifiedToolCall``
        # — the chokepoint refuses to execute unclassified actions.
        self.classifier = classifier
        # The path boundary guard (TD-602).  Path-bearing tools are
        # refused before execution when the guard is missing or the
        # target crosses the boundary.
        self.path_guard = path_guard
        # The checkpoint committer (TD-705).  Successful mutating tools
        # with path fields are checkpointed to the session branch; a
        # missing checkpointer silently skips checkpointing (tests wire
        # one explicitly, agent_loop wires one by default).
        self.checkpointer = checkpointer
        # Memory HEAD commits (TD-2104). Not the checkpointer: that
        # module never touches HEAD. Missing means tests that do not
        # care about memory commits skip this step.
        self.memory_committer = memory_committer
        # The decisions ledger (TD-704).  Class A/B decisions that
        # execute append to .tst/autonomy/DECISIONS.md.
        self.ledger = ledger
        # The approval policy gate (TD-801/802).  ``policy`` defaults to
        # rule-free class defaults; ``workspace`` grounds path summaries
        # for rule matching.  A call resolving to ``ask`` without an
        # ``approval_handler`` raises ``UnclassifiedToolCall`` — the gate
        # is part of the chokepoint, not optional.
        self.policy = policy
        self.approval_handler = approval_handler
        self.workspace = workspace
        # TD-804: live read of the machine-wide skip-all bit.  A callable
        # so toggling the setting mid-session does not require rewiring
        # every dispatcher.  Tests leave it None (off).
        self.skip_all_fn: Callable[[], bool] | None = None
        self._handlers: dict[str, Callable[..., Awaitable[str]]] = {}

    # ── Handler registration ──────────────────────────────────────────

    def register_handler(self, name: str, handler: Callable[..., Awaitable[str]]) -> None:
        """Register a handler for a tool.

        The handler is an async callable that receives ``session`` and
        ``tool_call_id`` plus the parsed arguments (all as keyword
        arguments) and returns a string result.

        Raises:
            KeyError: If the tool name is not registered in the registry.
        """
        if self.registry.get(name) is None:
            raise KeyError(
                f"Cannot register handler for unknown tool '{name}'. "
                f"Register the tool in the registry first."
            )
        self._handlers[name] = handler

    # ── Single dispatch ───────────────────────────────────────────────

    async def dispatch(
        self,
        tool_call_id: str,
        name: str,
        arguments: dict[str, Any],
        session: Any = None,
    ) -> ToolResult:
        """Validate and execute a single tool call.

        Args:
            tool_call_id: The tool call ID from the model.
            name: The tool name.
            arguments: Parsed JSON arguments (dict).
            session: Optional session object, passed to the handler.

        Returns:
            A ``ToolResult`` with the execution output or error.
        """
        # 1. Resolve tool in registry
        tool = self.registry.get(name)
        if tool is None:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                status="error",
                output=f"Unknown tool '{name}'. Available tools: "
                f"{', '.join(t.name for t in self.registry.list_tools())}",
                error_code="unknown_tool",
            )

        # 2. Validate arguments against schema
        validation_error = self._validate_args(tool, arguments)
        if validation_error is not None:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                status="error",
                output=validation_error,
                error_code="invalid_arguments",
            )

        # 3. Execute handler
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                status="error",
                output=f"Tool '{name}' is registered but has no handler. "
                f"This is a server configuration issue.",
                error_code="no_handler",
            )

        # 3.1 Decision classifier chokepoint (TD-702, prime §2.6).
        # Classification precedes execution.  A call that reaches this
        # point without a classifier attached is a bypass and raises.
        if self.classifier is None:
            raise UnclassifiedToolCall(name)
        # Copy then join workspace-relative paths before classification.
        # The classifier and the handler must see the same target the
        # guard will judge — not a cwd-relative Path that only works when
        # the process happens to be sitting in the workspace (TD-608).
        arguments = dict(arguments)
        if tool.path_fields and self.path_guard is not None:
            for field in tool.path_fields:
                raw = arguments.get(field)
                if isinstance(raw, str):
                    arguments[field] = str(self.path_guard.canonicalize(raw))
        request = build_decision_request(tool, arguments)
        classification = await self.classifier.classify(request)
        decision_class = classification.decision_class

        # 3.2 Path boundary enforcement (TD-602, security-critical).
        # Path-bearing tools are refused before the handler runs: no
        # guard attached is a bypass; a target crossing the boundary is a
        # refusal with a clear error.  Handlers receive validated paths.
        # Canonical write targets are kept for checkpointing (TD-705).
        canonical_writes: dict[str, Path] = {}
        if tool.path_fields:
            if self.path_guard is None:
                raise UnclassifiedToolCall(
                    f"path-bearing tool '{name}' reached execution without a path guard"
                )
            try:
                for field in tool.path_fields:
                    raw = arguments.get(field)
                    if not isinstance(raw, str):
                        continue  # absent/optional path field
                    if tool.mutates:
                        canonical_writes[field] = self.path_guard.check_write(raw)
                    else:
                        self.path_guard.check_read(raw)
            except RefusalError as e:
                log.warning(
                    "boundary refusal",
                    extra={
                        "extra_fields": {
                            "tool_call_id": tool_call_id,
                            "tool": name,
                            "code": e.code,
                            "path": str(e.path),
                        }
                    },
                )
                # Boundary refusals are definitionally Class C (§12.2:
                # "anything the charter forbids"), even when the static
                # table classified the call differently (e.g. hardlinks).
                decision_class = DecisionClass.C
                return ToolResult(
                    tool_call_id=tool_call_id,
                    name=name,
                    status="error",
                    output=f"Refused: {e.reason}",
                    error_code="boundary_refusal",
                    decision_class=decision_class,
                )

        # ── Policy gate (TD-801/802).  Boundary refusals returned above;
        # policy resolves auto/ask/never from the decision class and the
        # workspace rules.  auto runs; never refuses with a structured
        # result; ask parks on the injected approval handler.  Reaching
        # ask without a handler is a chokepoint bypass and raises.
        # The classifier contract (TD-703) already defaults failure to B;
        # a missing class here fails toward asking, never acting.
        gate_class = decision_class if decision_class is not None else DecisionClass.B
        skip_all = self.skip_all_fn() if self.skip_all_fn is not None else False
        decision = resolve_explained(
            self.policy if self.policy is not None else PolicyConfig(),
            tool,
            arguments,
            gate_class,
            self.workspace,
            skip_all=skip_all,
        )
        if decision.effect == "never":
            log.warning(
                "policy refusal",
                extra={
                    "extra_fields": {
                        "tool_call_id": tool_call_id,
                        "tool": name,
                        "reason": decision.reason,
                    }
                },
            )
            return ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                status="error",
                output=f"Refused by policy: {decision.reason}",
                error_code="policy_denied",
                decision_class=decision_class,
            )
        if decision.effect == "ask":
            if self.approval_handler is None:
                raise UnclassifiedToolCall(
                    f"tool '{name}' requires approval but no approval handler is attached"
                )
            approval = await self.approval_handler(
                tool_call_id,
                tool,
                arguments,
                gate_class,
                format_summary(tool, arguments, self.workspace),
                decision.reason,
            )
            if not approval.approved:
                log.info(
                    "approval denied",
                    extra={
                        "extra_fields": {
                            "tool_call_id": tool_call_id,
                            "tool": name,
                            "message": approval.message,
                        }
                    },
                )
                return ToolResult(
                    tool_call_id=tool_call_id,
                    name=name,
                    status="error",
                    output=approval.message,
                    error_code="approval_denied",
                    decision_class=decision_class,
                )

        # 3.25 Diff snapshot (TD-604).  A successful mutation reports the
        # change it made: snapshot the canonical write targets before the
        # handler runs, then diff against their state afterwards.
        before_snapshots: dict[str, str | None] = {}
        if tool.mutates and canonical_writes:
            before_snapshots = {
                field: snapshot_text(canonical) for field, canonical in canonical_writes.items()
            }

        try:
            output = await handler(session=session, tool_call_id=tool_call_id, **arguments)
        except (HandlerRefusal, DesktopError) as e:
            log.info(
                "tool handler refused",
                extra={
                    "extra_fields": {
                        "tool_call_id": tool_call_id,
                        "tool": name,
                        "code": e.code,
                    }
                },
            )
            return ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                status="error",
                output=e.message,
                error_code=e.code,
                decision_class=decision_class,
            )
        except Exception as e:
            log.exception(
                "tool handler failed",
                extra={
                    "extra_fields": {
                        "tool_call_id": tool_call_id,
                        "tool": name,
                        "error": str(e),
                    }
                },
            )
            return ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                status="error",
                output=f"Tool '{name}' failed: {e}",
                error_code="handler_error",
            )

        # 3.2.1 Touch-tracking (TD-503).  The handler ran, so the call's
        #      path targets are genuinely in play: record them on the
        #      session so path-scoped steering rules can activate at the
        #      next assembly.  Refusals and handler errors returned above
        #      never reach here — only real touches count.
        if session is not None and tool.path_fields:
            record = getattr(session, "record_touched", None)
            if record is not None:
                touched = [
                    arguments[f] for f in tool.path_fields if isinstance(arguments.get(f), str)
                ]
                record(touched)

        # 3.3 Checkpoint commit (TD-705).  Successful path-bearing
        # mutations are committed to the session branch so every write is
        # attributable to a revertable commit.  Checkpointing is
        # best-effort: it must never fail the write itself.
        checkpoint_commit: str | None = None
        checkpoint_notice = None
        if (
            tool.mutates
            and canonical_writes
            and self.checkpointer is not None
            and session is not None
        ):
            try:
                outcome = await self.checkpointer.checkpoint(
                    list(canonical_writes.values()),
                    tool_name=name,
                    tool_call_id=tool_call_id,
                    decision_class=decision_class,
                    decision_rule=classification.rule.id if classification.rule else None,
                )
                checkpoint_commit = outcome.commit
                checkpoint_notice = outcome.notice
            except Exception:  # defense in depth; checkpoint traps its own errors
                log.exception(
                    "checkpoint raised unexpectedly",
                    extra={"extra_fields": {"tool_call_id": tool_call_id, "tool": name}},
                )

        memory_notice = None
        if tool.mutates and canonical_writes and self.memory_committer is not None:
            try:
                memory_outcome = await self.memory_committer.commit(list(canonical_writes.values()))
                memory_notice = memory_outcome.notice
            except Exception:
                log.exception(
                    "memory commit raised unexpectedly",
                    extra={"extra_fields": {"tool_call_id": tool_call_id, "tool": name}},
                )

        # 3.4 Diff of the write (TD-604), for display on the tool_result.
        diff_text: str | None = None
        if tool.mutates and canonical_writes:
            sections: list[str] = []
            for field, canonical in canonical_writes.items():
                section = render_diff(
                    before_snapshots.get(field), snapshot_text(canonical), str(canonical)
                )
                if section:
                    sections.append(section)
            if sections:
                diff_text, _ = truncate_output("\n\n".join(sections), self.max_result_chars)

        # 4. Truncate
        truncated_output, truncated = truncate_output(output, self.max_result_chars)

        # 4.1 Decisions ledger (TD-704).  A Class A/B decision that
        #     executed appends to .tst/autonomy/DECISIONS.md and emits
        #     decision_logged.  Best-effort: the ledger must never fail
        #     the tool result.  Class A without a commit is refused by
        #     the ledger (an action that cannot be attributed to a
        #     commit is not Class A) and skipped, not misrecorded.
        if (
            decision_class is not None
            and decision_class.value in ("A", "B")
            and self.ledger is not None
            and session is not None
        ):
            try:
                dc = cast(Literal["A", "B"], decision_class.value)
                what = f"{name} {json.dumps(arguments, sort_keys=True)[:200]}"
                why = (
                    classification.reason
                    or (classification.rule.id if classification.rule else "")
                    or f"classified {dc}"
                )
                entry = LedgerEntry(
                    decision_class=dc,
                    what=what,
                    why=why,
                    commit=checkpoint_commit,
                )
                try:
                    await self.ledger.append(entry)
                except ValueError:
                    pass  # Class A without a commit — not logged as A (AC 3)
                else:
                    await session.event_log.add(
                        DecisionLoggedEvent(
                            session_id=session.id,
                            decision_class=dc,
                            what=entry.what,
                            why=entry.why,
                            commit=entry.commit,
                            seq=1,  # overwritten by the event log
                        )
                    )
            except Exception:
                log.exception(
                    "ledger append failed",
                    extra={
                        "extra_fields": {
                            "tool_call_id": tool_call_id,
                            "tool": name,
                        }
                    },
                )

        return ToolResult(
            tool_call_id=tool_call_id,
            name=name,
            status="success",
            output=truncated_output,
            truncated=truncated,
            decision_class=decision_class,
            checkpoint_commit=checkpoint_commit,
            checkpoint_notice=checkpoint_notice,
            memory_notice=memory_notice,
            diff=diff_text,
        )

    # ── Batch dispatch ────────────────────────────────────────────────

    async def dispatch_many(
        self,
        tool_calls: list[tuple[str, str, dict[str, Any]]],
        session: Any = None,
    ) -> list[ToolResult]:
        """Dispatch multiple tool calls, parallelising where safe.

        Args:
            tool_calls: List of ``(tool_call_id, name, arguments)`` tuples.
            session: Optional session object, passed to each handler.

        Returns:
            Results in the same order as the input tool calls.  Order is
            keyed on each call's *position* in ``tool_calls`` — never on
            ``tool_call_id``, which the client supplies and may repeat.
        """
        # Partition into parallel-safe and sequential, carrying each call's
        # input position so the two partitions reassemble into the caller's
        # order (TD-607).  Concatenating the partitions returned the batch
        # in partition order, which is not the order the model asked for.
        parallel_calls: list[tuple[int, str, str]] = []
        parallel_coros: list[Coroutine[Any, Any, ToolResult]] = []
        sequential_calls: list[tuple[int, str, str, dict[str, Any]]] = []

        for index, (tc_id, name, args) in enumerate(tool_calls):
            tool = self.registry.get(name)
            if tool is not None and tool.parallel_safe:
                parallel_calls.append((index, tc_id, name))
                parallel_coros.append(self.dispatch(tc_id, name, args, session))
            else:
                sequential_calls.append((index, tc_id, name, args))

        by_index: dict[int, ToolResult] = {}

        # Run parallel batch concurrently.  ``return_exceptions`` governs
        # what ``gather`` *returns*, so the outcomes are read from its
        # result list; ``task.result()`` would re-raise and take the whole
        # batch down with one failing call.
        if parallel_coros:
            outcomes: list[ToolResult | BaseException] = await asyncio.gather(
                *parallel_coros, return_exceptions=True
            )
            for (index, tc_id, name), outcome in zip(parallel_calls, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    # A chokepoint bypass (prime §2.6) and anything that is
                    # not an ordinary error stay loud, exactly as they do on
                    # the sequential path.  A dispatch that failed for one
                    # call must not decide the fate of its siblings.
                    if isinstance(outcome, UnclassifiedToolCall) or not isinstance(
                        outcome, Exception
                    ):
                        raise outcome
                    log.exception(
                        "concurrent dispatch failed",
                        exc_info=outcome,
                        extra={"extra_fields": {"tool_call_id": tc_id, "tool": name}},
                    )
                    by_index[index] = ToolResult(
                        tool_call_id=tc_id,
                        name=name,
                        status="error",
                        output=f"Concurrent dispatch failed: {outcome}",
                        error_code="concurrent_error",
                    )
                else:
                    by_index[index] = outcome

        # Run sequential batch one at a time
        for index, tc_id, name, args in sequential_calls:
            by_index[index] = await self.dispatch(tc_id, name, args, session)

        return [by_index[index] for index in range(len(tool_calls))]

    # ── Validation ────────────────────────────────────────────────────

    @staticmethod
    def _validate_args(tool: Tool, arguments: dict[str, Any]) -> str | None:
        """Validate *arguments* against the tool's JSON Schema.

        Returns an error message string, or ``None`` if valid.
        """
        if not tool.parameters:
            # No schema means no parameters expected
            if arguments:
                return f"Tool '{tool.name}' accepts no arguments, "
                f"but received: {json.dumps(arguments)}"
            return None

        try:
            validate_schema(instance=arguments, schema=tool.parameters)
        except SchemaError as e:
            path = " → ".join(str(p) for p in e.path) if e.path else "(root)"
            return (
                f"Invalid arguments for '{tool.name}': "
                f"{path}: {e.message}. "
                f"Expected schema: {json.dumps(tool.parameters, indent=2)}"
            )
        return None
