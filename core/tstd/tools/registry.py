"""Tool registry — explicit registration of built-in tools.

Each tool declares its name, description, JSON Schema parameters,
side-effect class, and parallel-safety flag.  The registry is the
single source of truth for what tools exist and how to call them.

Registration is explicit in v0.1 — no dynamic discovery (TD-601).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..provider import FunctionDefinition, ToolDefinition

# ── Side-effect classification ──────────────────────────────────────────

SideEffectClass = Literal["auto", "ask", "never"]
"""Classification for the approval gate:

- ``auto``: no user approval needed (read-only, safe).
- ``ask``: prompt the user for approval before execution.
- ``never``: never allow the tool to be called (reserved for v0.1).
"""


# ── Tool model ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Tool:
    """A registered tool that the model may call.

    Attributes:
        name: Unique tool name used in function-calling protocol.
        description: Natural-language description of what the tool does.
        parameters: JSON Schema object describing the parameters.  Must
            include ``type`` and ``properties`` at minimum.
        side_effect_class: Approval gate classification.
        parallel_safe: If True, multiple instances of this tool may be
            executed concurrently without conflict.
        path_fields: Argument keys holding file paths the tool reads or
            writes, used to build a ``DecisionRequest`` for the classifier.
        host_fields: Argument keys holding network hosts the tool reaches.
        mutates: True when the tool changes state (a write), so its
            ``path_fields`` are treated as write targets.
    """

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    side_effect_class: SideEffectClass = "auto"
    parallel_safe: bool = False
    # Decision-classifier metadata (TD-702): which argument keys are file
    # paths, which are network hosts, and whether the tool mutates state.
    # The classifier reduces a call to these signals so the rule table can
    # classify it without a model call.  Explicit, per-tool — no heuristics.
    path_fields: tuple[str, ...] = ()
    host_fields: tuple[str, ...] = ()
    mutates: bool = False

    def __post_init__(self) -> None:
        """Validate basic invariants."""
        if not self.name:
            raise ValueError("Tool name must be non-empty")
        if self.side_effect_class not in ("auto", "ask", "never"):
            raise ValueError(
                f"Tool '{self.name}': side_effect_class must be 'auto', 'ask', or 'never', "
                f"got {self.side_effect_class!r}"
            )
        if not isinstance(self.parameters, dict):
            raise ValueError(
                f"Tool '{self.name}': parameters must be a dict, "
                f"got {type(self.parameters).__name__}"
            )
        if self.parameters.get("type") != "object":
            raise ValueError(
                f"Tool '{self.name}': parameters.type must be 'object', "
                f"got {self.parameters.get('type')!r}"
            )
        if "properties" not in self.parameters:
            raise ValueError(f"Tool '{self.name}': parameters must include a 'properties' key")


# ── Structured error for unknown tools ─────────────────────────────────


@dataclass(frozen=True)
class UnknownToolError:
    """Structured error returned when a tool name is not registered.

    The model receives this so it can correct itself, rather than having
    the turn fail.
    """

    name: str
    message: str = ""

    def __post_init__(self) -> None:
        if not self.message:
            object.__setattr__(
                self,
                "message",
                f"Unknown tool '{self.name}'. Available tools: see the list of registered tools.",
            )


# ── Tool registry ──────────────────────────────────────────────────────


class ToolRegistry:
    """Registry of all available tools.

    Registration is explicit — tools must be registered before they can
    be called or exposed to the model.  No dynamic discovery in v0.1.

    Usage::

        registry = ToolRegistry()
        registry.register(Tool(name="fs_read", ...))
        tool = registry.get("fs_read")          # Tool or None
        tool = registry.require("fs_read")      # Tool or raises KeyError
        defs = registry.to_provider_definitions()  # list[ToolDefinition]
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool.  Replaces any existing tool with the same name."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """Look up a tool by name.  Returns ``None`` if not found."""
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        """Look up a tool by name.

        Raises:
            KeyError: If the tool is not registered.
        """
        if name not in self._tools:
            raise KeyError(name)
        return self._tools[name]

    def list_tools(self) -> list[Tool]:
        """Return all registered tools, sorted by name."""
        return sorted(self._tools.values(), key=lambda t: t.name)

    @property
    def count(self) -> int:
        return len(self._tools)

    def to_provider_definitions(self) -> list[ToolDefinition]:
        """Convert all registered tools to provider-format definitions.

        The returned list can be passed directly to
        :class:`tstd.provider.ChatCompletionRequest` as the ``tools``
        argument.
        """
        return [
            ToolDefinition(
                type="function",
                function=FunctionDefinition(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.parameters,
                ),
            )
            for tool in self.list_tools()
        ]

    def to_dict_list(self) -> list[dict[str, Any]]:
        """Return tools as a list of plain dicts (for serialization / debugging)."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "side_effect_class": tool.side_effect_class,
                "parallel_safe": tool.parallel_safe,
            }
            for tool in self.list_tools()
        ]


# ── Built-in tools ──────────────────────────────────────────────────────


def _register_builtins(registry: ToolRegistry) -> None:
    """Register the built-in tools that ship with TST Desk.

    These are the filesystem and shell tools that the agent loop needs
    to function.  Extensions (MCP, etc.) are registered separately.
    """
    registry.register(
        Tool(
            name="fs_read",
            description="Read the contents of a file at the given path. "
            "Returns the file content, optionally limited to a number of lines.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to read",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to return (0 = all)",
                        "default": 0,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Starting line number (1-based, 0 = start)",
                        "default": 0,
                    },
                },
                "required": ["path"],
            },
            side_effect_class="auto",
            parallel_safe=True,
            path_fields=("path",),
        )
    )

    registry.register(
        Tool(
            name="fs_write",
            description="Write content to a file at the given path. "
            "Creates parent directories if they do not exist. "
            "By default, overwrites the file; use append=True to append.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to write",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write to the file",
                    },
                    "append": {
                        "type": "boolean",
                        "description": "If True, append to the file instead of overwriting",
                        "default": False,
                    },
                },
                "required": ["path", "content"],
            },
            side_effect_class="ask",
            parallel_safe=False,
            path_fields=("path",),
            mutates=True,
        )
    )

    registry.register(
        Tool(
            name="shell",
            description="Execute a shell command and return its output. "
            "The command runs in the session's workspace directory. "
            "Use this for running scripts, compilers, tests, and other CLI tools.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                    "timeout_secs": {
                        "type": "integer",
                        "description": "Maximum time in seconds before the command is killed",
                        "default": 30,
                    },
                },
                "required": ["command"],
            },
            side_effect_class="ask",
            parallel_safe=False,
            mutates=True,
        )
    )


def create_registry() -> ToolRegistry:
    """Create a new ToolRegistry with all built-in tools pre-registered."""
    registry = ToolRegistry()
    _register_builtins(registry)
    return registry
