"""Tools — built-in tools for the agent loop.

The tool registry (TD-601) manages explicit registration of all tools.
The tool dispatcher (TD-402) handles parsing, validation, and execution
of tool calls from the model.
"""

from .dispatch import ToolDispatcher, ToolResult, ValidationError, classify_tool_call
from .registry import Tool, ToolRegistry, UnknownToolError, create_registry

__all__ = [
    "Tool",
    "ToolDispatcher",
    "ToolRegistry",
    "ToolResult",
    "UnknownToolError",
    "ValidationError",
    "classify_tool_call",
    "create_registry",
]
