"""Tools — built-in tools for the agent loop.

The tool registry (TD-601) manages explicit registration of all tools.
The tool dispatcher (TD-402) handles parsing, validation, and execution
of tool calls from the model.
"""

from .dispatch import (
    ToolDispatcher,
    ToolResult,
    UnclassifiedToolCall,
    ValidationError,
    build_decision_request,
)
from .handlers import fs_list, fs_read, register_builtin_handlers
from .registry import Tool, ToolRegistry, UnknownToolError, create_registry
from .write import fs_edit, fs_write

__all__ = [
    "Tool",
    "ToolDispatcher",
    "ToolRegistry",
    "ToolResult",
    "UnclassifiedToolCall",
    "UnknownToolError",
    "ValidationError",
    "build_decision_request",
    "create_registry",
    "fs_edit",
    "fs_list",
    "fs_read",
    "fs_write",
    "register_builtin_handlers",
]
