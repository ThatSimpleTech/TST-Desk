"""Tools — built-in tools for the agent loop.

The tool registry (TD-601) manages explicit registration of all tools.
The tool dispatcher (TD-402) handles parsing, validation, and execution
of tool calls from the model.

Built-in tools are registered in :func:`registry.create_registry`.
"""

from .registry import Tool, ToolRegistry, UnknownToolError, create_registry

__all__ = [
    "Tool",
    "ToolRegistry",
    "UnknownToolError",
    "create_registry",
]
