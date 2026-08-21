"""A fake stdio MCP server for tests (TD-4401).

Generalizes the ``_FAKE_MCP`` script from test_desktop_tools.py: same
newline-delimited JSON-RPC wire behavior, but its tool catalog comes
from a JSON blob on argv so each test can advertise its own tools.
Run as ``[sys.executable, "-c", SCRIPT, json_spec]`` — build the argv
with :func:`spec` (under ``-c``, ``sys.argv[1]`` is the first argument
after the script text).
"""

from __future__ import annotations

import json
import sys
from typing import Any

SCRIPT = """
import json, sys

spec = json.loads(sys.argv[1])
tools = spec.get("tools", [])
fail_tools = set(spec.get("fail_tools", []))

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": spec.get("name", "fake-mcp"), "version": "0"},
            },
        }))
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}))
    elif method == "tools/call":
        name = (msg.get("params") or {}).get("name")
        if name in fail_tools:
            print(json.dumps({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [{"type": "text", "text": f"tool {name} failed"}],
                    "isError": True,
                },
            }))
        else:
            args = json.dumps((msg.get("params") or {}).get("arguments") or {})
            print(json.dumps({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [
                        {"type": "text", "text": f"{name} done"},
                        {"type": "text", "text": args},
                    ]
                },
            }))
    sys.stdout.flush()
"""


def spec(
    tools: list[dict[str, Any]] | None = None,
    *,
    fail_tools: list[str] | None = None,
    name: str = "fake-mcp",
) -> list[str]:
    """Build the full argv for one fake server: interpreter + script + spec."""
    payload = {"name": name, "tools": tools or [], "fail_tools": fail_tools or []}
    return [sys.executable, "-c", SCRIPT, json.dumps(payload)]
