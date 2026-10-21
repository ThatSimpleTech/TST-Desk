"""Reading an MCP server's HTTP answers (TD-5136).

Pure parsing, kept apart from the HTTP client so it opens no transport:
a JSON body, an SSE stream's event data, or an error status.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from .era import NotModernError, rpc_error
from .errors import McpError

#: Handles one server message on a response stream that is not the answer.
OnMessage = Callable[[dict[str, Any]], Awaitable[None]]


class BadAnswer(McpError):
    """A reply that is not JSON-RPC: during the probe, a legacy verdict."""


def parse_message(raw: str | bytes) -> dict[str, Any] | None:
    """*raw* as a JSON object, or None."""
    try:
        parsed: Any = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def result_of(message: dict[str, Any]) -> Any:
    """The ``result`` of a JSON-RPC response; its ``error`` raised."""
    if "error" in message:
        raise rpc_error(message["error"])
    return message.get("result")


def json_answer(body: bytes, req_id: Any) -> Any:
    """The result in a plain JSON response body."""
    if not body:
        raise BadAnswer("MCP HTTP response was empty")
    message = parse_message(body)
    if message is None:
        raise BadAnswer("MCP HTTP response was not a JSON object")
    if message.get("id") not in (req_id, None):
        raise McpError("MCP HTTP response id did not match the request")
    return result_of(message)


async def sse_answer(events: AsyncIterator[str], req_id: Any, on_message: OnMessage) -> Any:
    """The result at the end of an SSE response; other messages go to *on_message*."""
    async for data in events:
        message = parse_message(data)
        if message is None:
            continue
        if "method" not in message and message.get("id") == req_id:
            return result_of(message)
        await on_message(message)
    raise McpError("MCP SSE stream ended without an answer")


def refusal(status: int, body: bytes) -> McpError:
    """The error for an HTTP status of 400 or more.

    A JSON-RPC error body is the server's own answer. Without one, 400,
    404 and 405 are what a legacy server says to the era probe.
    """
    message = parse_message(body)
    if message is not None and "error" in message:
        return rpc_error(message["error"])
    text = f"MCP HTTP request failed: HTTP {status}"
    if status in (400, 404, 405):
        return NotModernError(text)
    return McpError(text)
