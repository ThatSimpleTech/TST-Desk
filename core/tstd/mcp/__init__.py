"""MCP extension servers loaded from config (TD-4401).

Static loading, per the architecture guide: the servers named in
``mcp.servers`` start once per daemon and are never discovered or
hot-reloaded. A server that fails to start is recorded and reported —
a doctor row and a per-session ``mcp_state`` event — never a dead
daemon. Contributed tools register into the per-session registry like
builtins and go through the same classifier chokepoint (TD-702);
provenance records where a tool came from but gates nothing.
"""

from .manager import (
    McpError,
    McpManager,
    McpServerStatus,
    McpTool,
    register_mcp_tools,
)

__all__ = [
    "McpError",
    "McpManager",
    "McpServerStatus",
    "McpTool",
    "register_mcp_tools",
]
