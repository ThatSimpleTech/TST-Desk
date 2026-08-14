"""Tool dispatch — parse, validate, and execute tool calls.

The dispatcher resolves tool names against the registry, validates
arguments against each tool's JSON Schema, executes handlers, and
returns structured results.  Parallel-safe tools run concurrently.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
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
from ..logging import get_logger
from ..protocol import DecisionLogged as DecisionLoggedEvent
from .boundary import PathGuard, RefusalError
from .diff import render_diff, snapshot_text
from .registry import Tool, ToolRegistry
from .results import ToolResult, ValidationError, truncate_output

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
    engine falls into by default.
    """


def build_decision_request(tool: Tool, arguments: dict[str, Any]) -> DecisionRequest:
    """Reduce a tool call to the signals the decision classifier needs.

    Uses the tool's declared ``path_fields`` / ``host_fields`` / ``mutates``
    metadata (TD-702) — never heuristics over raw argument text.  Read
    tools expose their ``path_fields`` as reads; mutating tools expose them
    as write targets.
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
        ledger: DecisionLedger | None = None,
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
        # The decisions ledger (TD-704).  Class A/B decisions that
        # execute append to .tst/autonomy/DECISIONS.md.
        self.ledger = ledger
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
            Results in the same order as the input tool calls.
        """
        # Partition into parallel-safe and sequential
        parallel_batch: list[asyncio.Task[ToolResult]] = []
        sequential_batch: list[tuple[str, str, dict[str, Any]]] = []

        for tc_id, name, args in tool_calls:
            tool = self.registry.get(name)
            if tool is not None and tool.parallel_safe:
                task = asyncio.create_task(self.dispatch(tc_id, name, args, session))
                parallel_batch.append(task)
            else:
                sequential_batch.append((tc_id, name, args))

        # Run parallel batch concurrently
        parallel_results: list[ToolResult] = []
        if parallel_batch:
            await asyncio.gather(*parallel_batch, return_exceptions=True)
            for task in parallel_batch:
                result = task.result()
                if isinstance(result, Exception):
                    result = ToolResult(
                        tool_call_id="",
                        name="",
                        status="error",
                        output=f"Concurrent dispatch failed: {result}",
                        error_code="concurrent_error",
                    )
                parallel_results.append(result)

        # Run sequential batch one at a time
        sequential_results: list[ToolResult] = []
        for tc_id, name, args in sequential_batch:
            result = await self.dispatch(tc_id, name, args, session)
            sequential_results.append(result)

        return parallel_results + sequential_results

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
