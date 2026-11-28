"""Text the agent types is never kept, only how much of it there was (CU-08).

``desktop_type`` and ``browser_type`` put text into a window: a password,
a token, a reply. The tool call that carries it was written in full to the
event log, the saved conversation, the audit log and the workspace's
decisions ledger. Every one of those now records ``[26 characters typed]``
instead. The call that runs keeps the real text: only what is recorded
changes.
"""

from __future__ import annotations

import json
from typing import Any

#: Tool name (or name suffix, for an MCP server's tool) to its text argument.
_TYPED = {"desktop_type": "text", "browser_type": "text"}
_MCP_SUFFIX = "type_text"


def _field(name: str) -> str | None:
    if name in _TYPED:
        return _TYPED[name]
    if name == _MCP_SUFFIX or name.endswith("__" + _MCP_SUFFIX):
        return "text"
    return None


def typed_marker(text: str) -> str:
    return f"[{len(text)} characters typed]"


def mask_typed_text(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """*arguments* with the typed text replaced by its length, when *name* types."""
    field = _field(name)
    value = arguments.get(field) if field else None
    if field is None or not isinstance(value, str):
        return arguments
    return {**arguments, field: typed_marker(value)}


def mask_typed_json(name: str, arguments: str) -> str:
    """The same for a tool call's JSON argument string, as the conversation holds it."""
    if _field(name) is None:
        return arguments
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return arguments
    if not isinstance(parsed, dict):
        return arguments
    return json.dumps(mask_typed_text(name, parsed), ensure_ascii=False)
