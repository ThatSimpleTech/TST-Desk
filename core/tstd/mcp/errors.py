"""Typed failures for user-listed MCP servers (TD-4401)."""

from __future__ import annotations


class McpError(Exception):
    """A handshake, transport, or JSON-RPC failure.

    ``fix`` is copy for a doctor row when the failure is something the
    user can change in config. Absent on transient call errors.
    """

    def __init__(self, message: str, *, fix: str | None = None) -> None:
        super().__init__(message)
        self.fix = fix
