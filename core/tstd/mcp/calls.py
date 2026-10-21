"""Call machinery the stdio and HTTP clients share (TD-5105, TD-5136).

A ``tools/call`` may run as long as the server keeps reporting progress
for its token, up to a hard cap; silence for the idle timeout ends it.
Both transports feed :func:`watch_call` the same way, so an HTTP call
honours the same idle reset as a stdio one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .era import METHOD_NOT_FOUND
from .errors import McpError
from .jsonrpc import McpRemoteTool, parse_tools_list

T = TypeVar("T")

#: Called when the server says its tool list changed.
ToolsChanged = Callable[[], None]

#: ``tools/list`` pages read before giving up on a server that never ends.
MAX_TOOL_PAGES = 50


class CallTimedOut(Exception):
    """A tools/call outlived its idle timeout or hard cap."""


async def watch_call(
    work: asyncio.Future[T],
    progressed: asyncio.Event,
    *,
    idle: float,
    hard: float,
) -> T:
    """*work*'s result, ending it when it goes quiet or runs too long.

    *progressed* is set by whoever reads the server's messages, once per
    progress notification for this call's token. Raises
    :class:`CallTimedOut` (with the reason) after cancelling *work*.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    hard_deadline = started + hard
    idle_deadline = started + idle
    try:
        while True:
            now = loop.time()
            if now >= hard_deadline:
                raise CallTimedOut(f"no result after {hard:g} s (max_call_seconds)")
            if now >= idle_deadline:
                raise CallTimedOut(f"no result or progress for {idle:g} s (timeout_seconds)")
            waiter = asyncio.ensure_future(progressed.wait())
            watched: list[asyncio.Future[Any]] = [work, waiter]
            try:
                await asyncio.wait(
                    watched,
                    timeout=min(idle_deadline, hard_deadline) - now,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                waiter.cancel()
            if work.done():
                return work.result()
            if progressed.is_set():
                progressed.clear()
                idle_deadline = loop.time() + idle
    finally:
        if not work.done():
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await work


def tool_call_params(name: str, arguments: dict[str, Any], token: str) -> dict[str, Any]:
    """``tools/call`` params with a progress token (TD-5105)."""
    return {"name": name, "arguments": arguments, "_meta": {"progressToken": token}}


def checked_result(result: Any) -> Any:
    """*result*, unless the server asked tstd for input it never gives.

    A modern server asks for sampling, elicitation or roots by returning
    ``resultType: input_required``. tstd declares none of those, so such a
    result is a failed call, not an answer.
    """
    if isinstance(result, dict) and result.get("resultType") == "input_required":
        raise McpError(
            "the MCP server asked for input tstd does not provide (sampling, elicitation or roots)"
        )
    return result


async def list_all_tools(
    request: Callable[[dict[str, Any] | None], Awaitable[Any]],
) -> list[McpRemoteTool]:
    """Every page of ``tools/list``; *request* sends one with the given params."""
    tools: list[McpRemoteTool] = []
    cursor: str | None = None
    for _ in range(MAX_TOOL_PAGES):
        result = await request({"cursor": cursor} if cursor else None)
        tools.extend(parse_tools_list(result))
        next_cursor = result.get("nextCursor") if isinstance(result, dict) else None
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            return tools
        cursor = next_cursor
    return tools


def reply_to_server_request(message: dict[str, Any]) -> dict[str, Any]:
    """tstd's answer to a request a legacy server sent the client.

    ``ping`` succeeds. Anything else is a method tstd does not offer: it
    declared no client capabilities, so a server has no cause to ask.
    """
    if message.get("method") == "ping":
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {}}
    return {
        "jsonrpc": "2.0",
        "id": message.get("id"),
        "error": {"code": METHOD_NOT_FOUND, "message": "Method not found"},
    }


def is_tools_changed(message: dict[str, Any]) -> bool:
    return message.get("method") == "notifications/tools/list_changed"
