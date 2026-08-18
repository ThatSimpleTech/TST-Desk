"""tst-cu-mcp: a local computer-use MCP server (vision + control).

Supports macOS and Windows; the platform-specific code lives in
:mod:`tst_cu_mcp.backends` behind one interface.

The package exposes a single console entry point, :func:`main`, which starts the
MCP server on the stdio transport. All heavy imports (the MCP SDK, pyobjc,
ctypes plumbing) are deferred so that importing the package stays cheap and
side-effect free.
"""

from __future__ import annotations

from tst_cu_mcp._version import __version__

__all__ = ["__version__", "main"]


def main() -> None:
    """Console entry point: build the server and serve over stdio."""
    from tst_cu_mcp.server import run

    run()
