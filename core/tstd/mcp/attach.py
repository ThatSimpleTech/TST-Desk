"""MCP tools in a session's registry, and keeping them current (TD-4401, TD-5136).

Each session gets its own registry and dispatcher. The supervisor holds
them weakly so that when a server says ``tools/list_changed`` it can swap
that server's tools in every live session; a closed session drops out on
its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import weakref
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol

from ..protocol import ToolProgress
from ..tools.dispatch import ToolDispatcher
from ..tools.registry import Tool, ToolRegistry
from .errors import McpError
from .jsonrpc import (
    ExcludedTool,
    McpRemoteTool,
    ProgressCallback,
    format_call_result,
    local_tool_name,
    tool_provenance,
)


class McpClient(Protocol):
    #: Advertised tools the transport refuses (TD-5136).
    excluded: list[ExcludedTool]
    on_tools_changed: Callable[[], None] | None

    @property
    def startup_seconds(self) -> float: ...
    async def start(self) -> None: ...
    async def list_tools(self) -> list[McpRemoteTool]: ...
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> Any: ...
    async def aclose(self) -> None: ...


#: Least time between two progress events for one call (TD-5105).  Every
#: notification still resets the idle timer; only the events are thinned.
PROGRESS_EVENT_INTERVAL = 0.5


def _progress_events(session: object, tool_call_id: str) -> ProgressCallback | None:
    """Turn ``notifications/progress`` into ``tool_progress`` events."""
    event_log = getattr(session, "event_log", None)
    session_id = getattr(session, "id", None)
    if event_log is None or not isinstance(session_id, str) or not tool_call_id:
        return None
    last_sent = -PROGRESS_EVENT_INTERVAL

    async def emit(progress: float, total: float | None, message: str | None) -> None:
        nonlocal last_sent
        now = asyncio.get_running_loop().time()
        if now - last_sent < PROGRESS_EVENT_INTERVAL:
            return
        last_sent = now
        await event_log.add(
            ToolProgress(
                session_id=session_id,
                tool_call_id=tool_call_id,
                progress=progress,
                total=total,
                message=message,
                seq=1,  # overwritten by the event log
            )
        )

    return emit


def _handler(client: McpClient, remote_name: str) -> Callable[..., Awaitable[str]]:
    async def call(*, session: object = None, tool_call_id: str = "", **arguments: Any) -> str:
        work = asyncio.ensure_future(
            client.call_tool(
                remote_name, arguments, on_progress=_progress_events(session, tool_call_id)
            )
        )
        wait_for_abort = getattr(session, "wait_for_turn_abort", None)
        if wait_for_abort is None:
            return format_call_result(await work)
        # Stopping the turn ends the call, as it does a shell command
        # (TD-5105).  Cancelling the call sends the server
        # notifications/cancelled.
        abort = asyncio.ensure_future(wait_for_abort())
        try:
            await asyncio.wait({work, abort}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            abort.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await abort
            stopped = not work.done()
            if stopped:
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await work
        if stopped:
            raise McpError(f"MCP tool {remote_name!r} stopped: the user stopped the turn")
        return format_call_result(work.result())

    return call


def register_server_tools(
    registry: ToolRegistry,
    dispatcher: ToolDispatcher,
    server_id: str,
    client: McpClient,
    tools: Iterable[McpRemoteTool],
    read_only: frozenset[str],
) -> set[str]:
    """Register *tools* and their handlers; the local names registered."""
    names: set[str] = set()
    for remote in tools:
        local = local_tool_name(server_id, remote.name)
        registry.register(
            Tool(
                name=local,
                description=remote.description,
                parameters=remote.parameters,
                side_effect_class="ask",
                parallel_safe=True,
                provenance=tool_provenance(server_id),
                # TD-4402: do not infer path/host keys from the
                # remote schema. Undeclared fields fail toward B.
                path_fields=(),
                host_fields=(),
                # TD-5123: only the user's list marks a tool
                # read-only for plan mode, never the server.
                read_only=remote.name in read_only,
            )
        )
        dispatcher.register_handler(local, _handler(client, remote.name))
        names.add(local)
    return names


def replace_server_tools(
    registry: ToolRegistry,
    dispatcher: ToolDispatcher,
    server_id: str,
    client: McpClient,
    tools: list[McpRemoteTool],
    read_only: frozenset[str],
) -> None:
    """Make *registry* hold exactly *tools* for *server_id*.

    A tool the server dropped is unregistered with its handler, so the
    model is no longer offered it and a late call is an unknown tool.
    An unchanged tool is left alone, keeping the registry version still
    when nothing really changed.
    """
    provenance = tool_provenance(server_id)
    current = {t.name: t for t in registry.list_tools() if t.provenance == provenance}
    wanted = {local_tool_name(server_id, t.name): t for t in tools}
    for name in current.keys() - wanted.keys():
        registry.unregister(name)
        dispatcher.unregister_handler(name)
    changed = [
        remote
        for local, remote in wanted.items()
        if local not in current
        or current[local].description != remote.description
        or current[local].parameters != remote.parameters
        or current[local].read_only != (remote.name in read_only)
    ]
    register_server_tools(registry, dispatcher, server_id, client, changed, read_only)


class AttachedSessions:
    """The (registry, dispatcher) pairs MCP tools went into, held weakly."""

    def __init__(self) -> None:
        self._pairs: list[tuple[weakref.ref[ToolRegistry], weakref.ref[ToolDispatcher]]] = []

    def add(self, registry: ToolRegistry, dispatcher: ToolDispatcher) -> None:
        self._pairs.append((weakref.ref(registry), weakref.ref(dispatcher)))

    def live(self) -> list[tuple[ToolRegistry, ToolDispatcher]]:
        out: list[tuple[ToolRegistry, ToolDispatcher]] = []
        kept: list[tuple[weakref.ref[ToolRegistry], weakref.ref[ToolDispatcher]]] = []
        for reg_ref, disp_ref in self._pairs:
            registry, dispatcher = reg_ref(), disp_ref()
            if registry is not None and dispatcher is not None:
                out.append((registry, dispatcher))
                kept.append((reg_ref, disp_ref))
        self._pairs = kept
        return out

    def clear(self) -> None:
        self._pairs = []
