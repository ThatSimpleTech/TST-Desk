"""Leave MCP tool schemas out of the request until searched (TD-5127).

Every MCP tool's schema used to go into every request. Above
``mcp.tool_search.threshold_percent`` of a tier's ``context_window`` they
now stay out: the request carries the builtin ``tool_search`` instead,
whose description names every MCP tool that can be loaded. A tool the
search finds, or one the conversation has already called, is loaded and
sent with its full definition on every later call of the session.

The deferred request is a plain ``tools`` array, so any OpenAI-compatible
endpoint takes it; nothing here asks the provider for a feature.

The decision is made once per tier and window for the life of the loop.
The token ratio a tier learns (TD-5007) moves the estimate between
calls, and a tool list that came and went with it would cost the
provider's prompt cache and confuse the model. Loaded tools go at the end
of the list in load order, so loading one leaves the earlier definitions
byte-identical.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from fnmatch import fnmatchcase

from ..compaction import estimate_tokens
from ..config import McpToolSearchConfig, TierConfig
from ..context.tokens import TokenCounter
from ..logging import get_logger
from ..provider import ChatMessage, FunctionDefinition, ToolDefinition
from ..tools.registry import Tool, ToolRegistry
from ..tools.tool_search import TOOL_SEARCH_NAME, is_mcp_tool, load_tools, server_of

log = get_logger("tstd.mcp.deferral")

#: Characters the tool-name catalog may add to ``tool_search``'s
#: description. A server past it is named with its tool count only.
CATALOG_BUDGET_CHARS = 4_000


def _name(definition: ToolDefinition) -> str | None:
    return definition.function.name if definition.function is not None else None


def catalog_text(tools: Sequence[Tool], budget: int = CATALOG_BUDGET_CHARS) -> str:
    """The loadable MCP tools by server, names only, within *budget*."""
    by_server: dict[str, list[str]] = {}
    for tool in tools:
        server = server_of(tool)
        by_server.setdefault(server, []).append(tool.name.removeprefix(f"{server}__"))
    lines = ["MCP tools you can load (full name <server>__<tool>):"]
    used = len(lines[0])
    for server in sorted(by_server):
        names = by_server[server]
        line = f"- {server}: {', '.join(names)}"
        if used + len(line) > budget:
            line = f"- {server}: {len(names)} tools, find them by keyword"
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def builtin_catalog_text(names: Sequence[str]) -> str:
    """Deferred builtin tools (TD-5173), names only."""
    return "Other tools you can load: " + ", ".join(sorted(names))


def deferred_builtins(
    definitions: Sequence[ToolDefinition], patterns: Sequence[str], exclude: frozenset[str]
) -> frozenset[str]:
    """Names in *definitions* a tier defers by pattern (TD-5173).

    ``tool_search`` itself and MCP tools (*exclude*) never match: the first
    is how the rest are found, the second have their own rule.
    """
    if not patterns:
        return frozenset()
    out = set()
    for definition in definitions:
        name = _name(definition)
        if name is None or name == TOOL_SEARCH_NAME or name in exclude:
            continue
        if any(fnmatchcase(name, pattern) for pattern in patterns):
            out.add(name)
    return frozenset(out)


def called_tools(messages: Iterable[ChatMessage], names: frozenset[str]) -> list[str]:
    """Names in *names* the assistant called in *messages*, first call first."""
    seen: list[str] = []
    for message in messages:
        for call in message.tool_calls or ():
            name = call.function.name if call.function is not None else None
            if name in names and name not in seen:
                seen.append(name)
    return seen


def _set_deferred_builtins(session: object, names: list[str]) -> None:
    """Tell ``tool_search`` which builtins it may load this step (TD-5173)."""
    if hasattr(session, "deferred_builtin_tools"):
        session.deferred_builtin_tools[:] = names  # type: ignore[attr-defined]


class McpToolDeferral:
    """Decides, per model call, which MCP tool definitions the request carries."""

    def __init__(self, registry: ToolRegistry, config: McpToolSearchConfig) -> None:
        self._config = config
        mcp_tools = [t for t in registry.list_tools() if is_mcp_tool(t)]
        self._mcp_names = frozenset(t.name for t in mcp_tools)
        self._catalog = catalog_text(mcp_tools) if mcp_tools else ""
        # (tier, context_window) -> defer. Latched: see the module docstring.
        self._decided: dict[tuple[str, int], bool] = {}

    @property
    def mcp_names(self) -> frozenset[str]:
        return self._mcp_names

    def offer(
        self,
        definitions: list[ToolDefinition] | None,
        *,
        session: object,
        tier: str,
        tier_cfg: TierConfig,
        counter: TokenCounter,
        messages: Sequence[ChatMessage],
    ) -> list[ToolDefinition] | None:
        """*definitions* as this call should send them."""
        if definitions is None:
            return None
        names = {_name(d) for d in definitions}
        builtin = deferred_builtins(definitions, tier_cfg.deferred_tools, self._mcp_names)
        defer_mcp = TOOL_SEARCH_NAME in names and self._defers(tier, tier_cfg, counter, definitions)
        if TOOL_SEARCH_NAME not in names or not (defer_mcp or builtin):
            _set_deferred_builtins(session, [])
            return [d for d in definitions if _name(d) != TOOL_SEARCH_NAME]
        _set_deferred_builtins(session, sorted(builtin))
        deferred = (self._mcp_names if defer_mcp else frozenset()) | builtin
        # A tool the conversation already called is in use: keep it
        # offered, also after a restart rebuilt the session from disk.
        load_tools(session, called_tools(messages, deferred))
        loaded = getattr(session, "loaded_mcp_tools", [])
        offered: list[ToolDefinition] = []
        by_name: dict[str, ToolDefinition] = {}
        for definition in definitions:
            name = _name(definition)
            if name in deferred:
                by_name[name] = definition
                continue
            if name == TOOL_SEARCH_NAME:
                parts = [self._catalog] if defer_mcp and self._catalog else []
                if builtin:
                    parts.append(builtin_catalog_text(sorted(builtin)))
                definition = self._with_catalog(definition, "\n".join(parts))
            offered.append(definition)
        offered.extend(by_name[n] for n in loaded if n in by_name)
        return offered

    def _with_catalog(self, definition: ToolDefinition, catalog: str) -> ToolDefinition:
        function = definition.function
        if function is None:
            return definition
        return ToolDefinition(
            type=definition.type,
            function=FunctionDefinition(
                name=function.name,
                description=f"{function.description}\n\n{catalog}",
                parameters=function.parameters,
            ),
        )

    def _defers(
        self,
        tier: str,
        tier_cfg: TierConfig,
        counter: TokenCounter,
        definitions: Sequence[ToolDefinition],
    ) -> bool:
        if not self._mcp_names or self._config.mode == "off":
            return False
        key = (tier, tier_cfg.context_window)
        decided = self._decided.get(key)
        if decided is not None:
            return decided
        mcp_defs = [d for d in definitions if _name(d) in self._mcp_names]
        tokens, _ = estimate_tokens([], counter, mcp_defs)
        limit = tier_cfg.context_window * self._config.threshold_percent / 100
        decided = self._config.mode == "always" or tokens > limit
        self._decided[key] = decided
        log.info(
            "mcp tool search decided",
            extra={
                "extra_fields": {
                    "tier": tier,
                    "deferred": decided,
                    "mcp_tools": len(self._mcp_names),
                    "mcp_tool_tokens": tokens,
                    "context_window": tier_cfg.context_window,
                    "threshold_percent": self._config.threshold_percent,
                    "mode": self._config.mode,
                }
            },
        )
        return decided
