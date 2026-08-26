"""User-listed MCP clients (TD-4401).

Servers come from ``config.yaml`` — there is no dynamic discovery.
This package speaks JSON-RPC itself; it is not the computer-use sidecar
and must not import ``tstd.desktop`` or ``tstd.autonomy.sandbox``.
"""

from .loader import McpServerStatus, McpSupervisor

__all__ = ["McpServerStatus", "McpSupervisor"]
