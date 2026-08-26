"""Shared MCP JSON-RPC helpers (TD-4401).

Handshake and tool naming live here so the stdio and HTTP clients stay
thin copies of the same protocol, not two dialects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "tstd"
CLIENT_VERSION = "0.1.0"
# Handshake / tools/list — doctor and session start must not hang a dead child.
HANDSHAKE_TIMEOUT = 8.0
# tools/call after the server is live.
CALL_TIMEOUT = 30.0
# Screenshots or large tool payloads; matches the CU sidecar's stream cap.
STREAM_LIMIT = 16 * 1024 * 1024


def initialize_params() -> dict[str, Any]:
    """Body for MCP ``initialize``."""
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
    }


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
                parameters=normalize_input_schema(item.get("inputSchema")),
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
