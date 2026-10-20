"""The ``tool_search`` tool: load deferred MCP tools on demand (TD-5127).

When MCP tool schemas would take too much of a tier's window, the loop
leaves them out of the request (:mod:`tstd.mcp.deferral`) and offers this
tool instead. It ranks the MCP tools against the model's words and adds
the matches to ``session.loaded_mcp_tools``; from the next model call on,
their full definitions are in the ``tools`` array like any other tool's.
Nothing here depends on a provider feature, so it works on any
OpenAI-compatible endpoint.

Searching reads only the daemon's own registry: the classifier's
``tool-search`` rule puts the call in Class A. Loading a tool grants it
nothing; each call to it still goes through the classifier.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import partial

from .dispatch import ToolDispatcher
from .registry import Tool, ToolRegistry
from .results import HandlerRefusal

TOOL_SEARCH_NAME = "tool_search"
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_CAP = 20
#: Longest description line a result shows per tool; the full schema is in
#: the next request's ``tools`` array, so the result need not repeat it.
RESULT_DESCRIPTION_CHARS = 160
SELECT_PREFIX = "select:"

TOOL_SEARCH_DESCRIPTION = (
    "Load MCP tools that are not in your tool list yet. Their names are listed below. "
    "query is a few keywords (e.g. 'create issue') or select:<name>[,<name>...] for exact "
    "names. The best matches load for the rest of the session: call them by name from "
    "your next step. Search again with other words if nothing fits."
)

TOOL_SEARCH_TOOL = Tool(
    name=TOOL_SEARCH_NAME,
    description=TOOL_SEARCH_DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "Keywords, or select:<name>[,<name>...] for exact tool names",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RESULTS_CAP,
                "description": f"Most tools to load (default {DEFAULT_MAX_RESULTS})",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    side_effect_class="auto",
    parallel_safe=False,
    mutates=False,
)


def register_tool_search_tool(registry: ToolRegistry) -> None:
    """Register the builtin ``tool_search`` tool. Call from ``create_registry``."""
    registry.register(TOOL_SEARCH_TOOL)


def register_tool_search_handler(dispatcher: ToolDispatcher) -> None:
    """Bind the handler to the dispatcher's registry, read at call time.

    MCP tools attach after the builtins, so the registry is read when
    the model searches, not when the handler is bound.
    """
    dispatcher.register_handler(
        TOOL_SEARCH_NAME, partial(tool_search, registry=dispatcher.registry)
    )


def is_mcp_tool(tool: Tool) -> bool:
    return tool.provenance is not None and tool.provenance.startswith("mcp:")


def server_of(tool: Tool) -> str:
    """The server id from ``mcp:<server-id>`` provenance."""
    return (tool.provenance or "").removeprefix("mcp:")


def _stem(word: str) -> str:
    # "issues" finds create_issue and "files" finds list_file.
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _words(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())]


def _score(tool: Tool, terms: Sequence[str]) -> tuple[float, int]:
    """(weighted score, distinct terms matched) of *tool* for *terms*."""
    name_words = set(_words(tool.name))
    name_text = tool.name.lower()
    desc_words = set(_words(tool.description))
    score = 0.0
    matched = 0
    for term in terms:
        if term in name_words:
            weight = 3.0
        elif len(term) > 2 and term in name_text:
            weight = 2.0
        elif term in desc_words:
            weight = 1.0
        else:
            continue
        score += weight
        matched += 1
    return score, matched


def _select(tools: Sequence[Tool], names: str) -> list[Tool]:
    """Exact picks: a full ``server__tool`` name, or a tool name one server has."""
    by_name = {t.name: t for t in tools}
    picked: list[Tool] = []
    for raw in names.split(","):
        name = raw.strip()
        if not name:
            continue
        found = by_name.get(name)
        if found is None:
            suffix = [t for t in tools if t.name.endswith(f"__{name}")]
            found = suffix[0] if len(suffix) == 1 else None
        if found is not None and found not in picked:
            picked.append(found)
    return picked


def search_tools(tools: Sequence[Tool], query: str, limit: int) -> list[Tool]:
    """The best *limit* of *tools* for *query*, best first."""
    text = query.strip()
    if text.lower().startswith(SELECT_PREFIX):
        return _select(tools, text[len(SELECT_PREFIX) :])[:limit]
    terms = list(dict.fromkeys(_words(text)))
    if not terms:
        return []
    ranked: list[tuple[float, int, str, Tool]] = []
    for tool in tools:
        score, matched = _score(tool, terms)
        if score > 0:
            ranked.append((-score, -matched, tool.name, tool))
    ranked.sort(key=lambda row: row[:3])
    return [row[3] for row in ranked[:limit]]


def _one_line(text: str) -> str:
    line = " ".join(text.split())
    if len(line) <= RESULT_DESCRIPTION_CHARS:
        return line
    return line[: RESULT_DESCRIPTION_CHARS - 1].rstrip() + "…"


def load_tools(session: object, names: Sequence[str]) -> list[str]:
    """Add *names* to the session's loaded MCP tools; return the new ones."""
    loaded = getattr(session, "loaded_mcp_tools", None)
    if not isinstance(loaded, list):
        return []
    added = [name for name in names if name not in loaded]
    loaded.extend(added)
    return added


async def tool_search(
    *,
    registry: ToolRegistry,
    session: object = None,
    tool_call_id: str = "",
    query: str,
    max_results: int | None = None,
) -> str:
    """Rank MCP tools against *query* and load the best matches."""
    if not isinstance(getattr(session, "loaded_mcp_tools", None), list):
        raise HandlerRefusal(
            "tool_search_unavailable", "Tool search is not available in this session."
        )
    candidates = [t for t in registry.list_tools() if is_mcp_tool(t)]
    if not candidates:
        return "No MCP tools are configured, so there is nothing to load."
    limit = min(max(max_results or DEFAULT_MAX_RESULTS, 1), MAX_RESULTS_CAP)
    found = search_tools(candidates, query, limit)
    if not found:
        return (
            f"No MCP tool matched {query.strip()!r}. Try other keywords, or "
            "select:<name> with a name from this tool's description."
        )
    added = set(load_tools(session, [t.name for t in found]))
    lines = [
        f"Loaded {len(found)} tool(s). Call them by name from your next step; "
        "they stay loaded for the rest of the session:"
    ]
    for tool in found:
        note = "" if tool.name in added else " (already loaded)"
        lines.append(f"- {tool.name}{note}: {_one_line(tool.description) or '(no description)'}")
    return "\n".join(lines)
