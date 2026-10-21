"""Streamable HTTP request headers for MCP 2026-07-28 (TD-5136).

A modern request mirrors its method, the tool or resource it names, and
any tool parameter the server marks with ``x-mcp-header`` into HTTP
headers, so a gateway can route on them without reading the body. A value
that is not plain visible ASCII goes as ``=?base64?…?=``. A tool whose
``x-mcp-header`` marks break the spec's rules is left out of the tool
list, as the spec requires of an HTTP client.
"""

from __future__ import annotations

import base64
import re
from typing import Any

from .era import PROTOCOL_VERSION_META

_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_PLAIN = re.compile(r"^[\x20-\x7e\t]*$")
_MAX_SAFE_INT = 2**53 - 1
#: Methods whose ``Mcp-Name`` header mirrors ``params.name`` or ``params.uri``.
_NAMED = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}
_MIRRORED_TYPES = {"string", "integer", "boolean"}


def header_value(value: str) -> str:
    """*value* as a header value, Base64-wrapped when it is not plain."""
    plain = (
        _PLAIN.match(value) is not None
        and value == value.strip()
        and not (value.startswith("=?base64?") and value.endswith("?="))
    )
    if plain:
        return value
    return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def _count_marks(node: Any) -> int:
    if isinstance(node, dict):
        own = 1 if "x-mcp-header" in node else 0
        return own + sum(_count_marks(v) for v in node.values())
    if isinstance(node, list):
        return sum(_count_marks(v) for v in node)
    return 0


def _reachable_marks(schema: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    """(property path, property schema) for marks reached through ``properties`` only."""
    found: list[tuple[tuple[str, ...], Any]] = []
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return found
    for key, sub in props.items():
        if not isinstance(sub, dict):
            continue
        if "x-mcp-header" in sub:
            found.append(((*path, key), sub))
        found.extend(_reachable_marks(sub, (*path, key)))
    return found


def mirrored_params(schema: dict[str, Any]) -> dict[tuple[str, ...], str]:
    """Property path to header name for each ``x-mcp-header`` mark.

    Raises ``ValueError`` naming the broken rule; the caller excludes the tool.
    """
    marks = _reachable_marks(schema)
    if len(marks) != _count_marks(schema):
        raise ValueError("x-mcp-header outside a plain properties chain")
    out: dict[tuple[str, ...], str] = {}
    seen: set[str] = set()
    for path, prop in marks:
        name = prop.get("x-mcp-header")
        if not isinstance(name, str) or not _TOKEN.match(name):
            raise ValueError(f"x-mcp-header {name!r} is not an HTTP header token")
        if name.lower() in seen:
            raise ValueError(f"x-mcp-header {name!r} is used twice")
        if prop.get("type") not in _MIRRORED_TYPES:
            raise ValueError(f"x-mcp-header {name!r} is on a {prop.get('type')!r} parameter")
        seen.add(name.lower())
        out[path] = name
    return out


def _param_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return str(value) if abs(value) <= _MAX_SAFE_INT else None
    if isinstance(value, str):
        return value
    return None


def _at(arguments: Any, path: tuple[str, ...]) -> Any:
    node = arguments
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def modern_headers(payload: dict[str, Any], mirrored: dict[tuple[str, ...], str]) -> dict[str, str]:
    """The request-metadata headers for a modern *payload*, else ``{}``."""
    params = payload.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    version = meta.get(PROTOCOL_VERSION_META) if isinstance(meta, dict) else None
    method = payload.get("method")
    if not isinstance(version, str) or not isinstance(method, str) or "id" not in payload:
        return {}
    assert isinstance(params, dict)
    headers = {"MCP-Protocol-Version": version, "Mcp-Method": method}
    named = params.get(_NAMED[method]) if method in _NAMED else None
    if isinstance(named, str):
        headers["Mcp-Name"] = header_value(named)
    if method == "tools/call":
        arguments = params.get("arguments")
        for path, name in mirrored.items():
            text = _param_text(_at(arguments, path))
            if text is not None:
                headers[f"Mcp-Param-{name}"] = header_value(text)
    return headers
