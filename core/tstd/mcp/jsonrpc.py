"""Shared MCP JSON-RPC helpers (TD-4401).

Tool naming, result shapes and the call watch live here so the stdio and
HTTP clients stay thin copies of the same protocol, not two dialects. The
version handshake is in :mod:`.era` (TD-5136).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import SchemaError
from jsonschema.validators import validator_for

# Handshake / tools/list — doctor and session start must not hang a dead child.
HANDSHAKE_TIMEOUT = 8.0
# tools/call after the server is live: the longest a call may go without
# a result or a progress notification (TD-5105).
CALL_TIMEOUT = 30.0
# Hard cap on one tools/call, however much progress it reports (TD-5105).
MAX_CALL_SECONDS = 600.0
# Screenshots or large tool payloads; matches the CU sidecar's stream cap.
STREAM_LIMIT = 16 * 1024 * 1024


#: Called with (progress, total, message) for each ``notifications/progress``.
ProgressCallback = Callable[[float, float | None, str | None], Awaitable[None]]


def cancelled_notification(request_id: int | str, reason: str) -> dict[str, Any]:
    """``notifications/cancelled`` for a request the client gave up on."""
    return {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": request_id, "reason": reason},
    }


def progress_update(
    message: dict[str, Any], token: str
) -> tuple[float, float | None, str | None] | None:
    """(progress, total, message) when *message* is progress for *token*."""
    if message.get("method") != "notifications/progress":
        return None
    params = message.get("params")
    if not isinstance(params, dict) or params.get("progressToken") != token:
        return None
    progress = params.get("progress")
    total = params.get("total")
    text = params.get("message")
    return (
        float(progress) if isinstance(progress, int | float) else 0.0,
        float(total) if isinstance(total, int | float) else None,
        text if isinstance(text, str) else None,
    )


def local_tool_name(server_id: str, remote_name: str) -> str:
    """Prefix so an MCP tool cannot collide with a builtin (e.g. ``fs_read``)."""
    return f"{server_id}__{remote_name}"


def tool_provenance(server_id: str) -> str:
    """Registry provenance tag: ``mcp:<server-id>``."""
    return f"mcp:{server_id}"


@dataclass(frozen=True)
class McpRemoteTool:
    """One tool advertised by ``tools/list``."""

    name: str
    description: str
    parameters: dict[str, Any]


def _raw_input_schema(raw: Any) -> dict[str, Any]:
    """The advertised schema as a dict; :func:`check_remote_tool` shapes it."""
    return dict(raw) if isinstance(raw, dict) else {"type": "object", "properties": {}}


def normalize_input_schema(raw: Any) -> dict[str, Any]:
    """Coerce an MCP ``inputSchema`` into the registry's object-schema shape."""
    if not isinstance(raw, dict):
        return {"type": "object", "properties": {}}
    schema = dict(raw)
    if schema.get("type") != "object":
        schema["type"] = "object"
    props = schema.get("properties")
    if not isinstance(props, dict):
        schema["properties"] = {}
    return schema


#: Function names strict OpenAI-compatible endpoints accept (TD-5104).
#: The registry name is ``<server>__<tool>``, so the prefix counts.
TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_ROOT_COMBINATORS = ("allOf", "anyOf", "oneOf")


@dataclass(frozen=True)
class ExcludedTool:
    """An advertised tool left out of the registry, and why (TD-5104)."""

    name: str
    reason: str


class UnusableSchema(ValueError):
    """An ``inputSchema`` that cannot be made into one object schema."""


def flatten_root_combinators(schema: dict[str, Any]) -> dict[str, Any]:
    """Fold root ``allOf`` / ``anyOf`` / ``oneOf`` into one object schema.

    Strict endpoints and grammar builders reject a combinator at the root
    of a function's parameters.  Each branch must be an object schema with
    no combinator of its own.  Properties are merged (first definition
    wins).  ``allOf`` requires what any branch requires; ``anyOf`` and
    ``oneOf`` require only what every branch requires, so the merged
    schema accepts every call one of the branches accepted.

    Raises:
        UnusableSchema: a branch is not a plain object schema.
    """
    if not any(key in schema for key in _ROOT_COMBINATORS):
        return schema
    out = dict(schema)
    props_raw = out.get("properties")
    props: dict[str, Any] = dict(props_raw) if isinstance(props_raw, dict) else {}
    req_raw = out.get("required")
    required: list[str] = list(req_raw) if isinstance(req_raw, list) else []
    for key in _ROOT_COMBINATORS:
        if key not in out:
            continue
        branches = out.pop(key)
        if not isinstance(branches, list) or not branches:
            raise UnusableSchema(f"root {key} is not a list of schemas")
        branch_required: list[set[str]] = []
        for branch in branches:
            if not isinstance(branch, dict):
                raise UnusableSchema(f"root {key} has a branch that is not a schema")
            if branch.get("type", "object") != "object":
                raise UnusableSchema(f"root {key} has a non-object branch")
            if any(k in branch for k in _ROOT_COMBINATORS):
                raise UnusableSchema(f"root {key} nests another combinator")
            branch_props = branch.get("properties")
            if isinstance(branch_props, dict):
                for name, sub in branch_props.items():
                    props.setdefault(name, sub)
            r = branch.get("required")
            branch_required.append(
                {x for x in r if isinstance(x, str)} if isinstance(r, list) else set()
            )
        if key == "allOf":
            wanted = set().union(*branch_required)
        else:
            wanted = set.intersection(*branch_required)
        required.extend(sorted(wanted))
    out["type"] = "object"
    out["properties"] = props
    deduped = list(dict.fromkeys(required))
    if deduped:
        out["required"] = deduped
    else:
        out.pop("required", None)
    return out


def check_remote_tool(server_id: str, tool: McpRemoteTool) -> McpRemoteTool | ExcludedTool:
    """Make *tool* safe for a strict endpoint, or say why it is left out.

    One bad tool must not fail every request that carries the tool list
    (TD-5104).  The name is checked as the registry will send it.
    """
    local = local_tool_name(server_id, tool.name)
    if not TOOL_NAME_PATTERN.match(local):
        if len(local) > 64:
            why = f"name is {len(local)} characters with the server prefix; the limit is 64"
        else:
            why = "name may only use letters, digits, _ and -"
        return ExcludedTool(name=tool.name, reason=why)
    try:
        schema = normalize_input_schema(flatten_root_combinators(tool.parameters))
        validator_for(schema).check_schema(schema)
        json.dumps(schema)
    except UnusableSchema as e:
        return ExcludedTool(name=tool.name, reason=f"input schema: {e}")
    except SchemaError as e:
        return ExcludedTool(name=tool.name, reason=f"input schema is invalid: {e.message}")
    except (TypeError, ValueError) as e:
        return ExcludedTool(name=tool.name, reason=f"input schema is not JSON: {e}")
    return McpRemoteTool(name=tool.name, description=tool.description, parameters=schema)


def sanitize_remote_tools(
    server_id: str, tools: list[McpRemoteTool]
) -> tuple[list[McpRemoteTool], list[ExcludedTool]]:
    """Split ``tools/list`` into usable tools and excluded ones."""
    kept: list[McpRemoteTool] = []
    excluded: list[ExcludedTool] = []
    for tool in tools:
        checked = check_remote_tool(server_id, tool)
        if isinstance(checked, ExcludedTool):
            excluded.append(checked)
        else:
            kept.append(checked)
    return kept, excluded


def parse_tools_list(result: Any) -> list[McpRemoteTool]:
    """Read ``tools/list`` result. Unknown shapes yield an empty list."""
    if not isinstance(result, dict):
        return []
    raw_tools = result.get("tools")
    if not isinstance(raw_tools, list):
        return []
    parsed: list[McpRemoteTool] = []
    for item in raw_tools:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        description = item.get("description")
        parsed.append(
            McpRemoteTool(
                name=name,
                description=description if isinstance(description, str) else "",
                parameters=_raw_input_schema(item.get("inputSchema")),
            )
        )
    return parsed


def format_call_result(result: Any) -> str:
    """Turn a ``tools/call`` result into the string a handler returns."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = [
                item["text"]
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            if parts:
                return "\n".join(parts)
        return json.dumps(result)
    return json.dumps(result, default=str)


def jsonrpc_error_text(error: Any) -> str:
    """Human-readable JSON-RPC error payload."""
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    return str(error)
