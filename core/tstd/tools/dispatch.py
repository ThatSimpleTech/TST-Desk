"""Tool dispatch — parse, validate, and execute tool calls.

The dispatcher resolves tool names against the registry, validates
arguments against each tool's JSON Schema, executes handlers, and
returns structured results.  Parallel-safe tools run concurrently.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import ValidationError as SchemaError
from jsonschema import validate as validate_schema

from ..logging import get_logger
from .registry import Tool, ToolRegistry

log = get_logger("tstd.dispatch")

# ── Result types ────────────────────────────────────────────────────────


@dataclass
class ToolResult:
    """The result of executing a tool call.

    Attributes:
        tool_call_id: Matches the tool call's ID from the model.
        name: The tool name.
        status: ``success`` or ``error``.
        output: Text output (or error message).
        truncated: Whether the output was truncated to the cap.
        error_code: For ``error`` status, a machine-readable code.
    """

    tool_call_id: str
    name: str
    status: str  # "success" | "error"
    output: str
    truncated: bool = False
    error_code: str | None = None


@dataclass
class ValidationError:
    """Arguments failed validation against the tool's schema.

    Returned to the model so it can correct itself.
    """

    tool_call_id: str
    name: str
    message: str


# ── Truncation ──────────────────────────────────────────────────────────

_TRUNCATION_MARKER = "\n\n┈─[truncated — results exceed output cap]─┈"


def truncate_output(output: str, max_chars: int) -> tuple[str, bool]:
    """Truncate *output* to *max_chars* with a visible truncation marker.

    Returns the (possibly truncated) text and a ``truncated`` flag.
    """
    if not max_chars or len(output) <= max_chars:
        return output, False
    truncated = output[: max_chars - len(_TRUNCATION_MARKER)]
    return truncated + _TRUNCATION_MARKER, True


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
    ) -> None:
        self.registry = registry
        self.max_result_chars = max_result_chars
        self._handlers: dict[str, Callable[..., Awaitable[str]]] = {}

    # ── Handler registration ──────────────────────────────────────────

    def register_handler(self, name: str, handler: Callable[..., Awaitable[str]]) -> None:
        """Register a handler for a tool.

        The handler is an async callable that receives the parsed arguments
        (as keyword arguments) and returns a string result.

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

        try:
            output = await handler(session=session, **arguments)
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

        # 4. Truncate
        truncated_output, truncated = truncate_output(output, self.max_result_chars)
        return ToolResult(
            tool_call_id=tool_call_id,
            name=name,
            status="success",
            output=truncated_output,
            truncated=truncated,
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


# ── Classifier placeholder (TD-702) ─────────────────────────────────────


def classify_tool_call(tool: Tool, arguments: dict[str, Any], session: Any = None) -> str:
    """Classify a tool call for the approval gate.

    Placeholder until TD-702 (decision classifier) is built.
    Currently returns the tool's static side_effect_class.

    Returns:
        One of ``"auto"``, ``"ask"``, ``"never"``.
    """
    return tool.side_effect_class
