"""Code navigation through a language server (TD-5139).

``lsp_definition``, ``lsp_references`` and ``lsp_symbols`` ask the
language server configured for a file (``lsp.servers`` in config.yaml)
where a symbol is defined, where it is used, and what a file declares.
Small local models find their way around a codebase far better with
these than by grepping.

A position is a 1-based ``line`` plus either the ``symbol`` on it (the
model names the word, TST Desk finds its column) or a 1-based
``column``. Results are workspace-relative ``path:line:col`` with the
line's text; a location outside the workspace (a library) shows its path
only, since the workspace wall would refuse reading it.

Each tool reads one in-workspace file and asks a local child process, so
the classifier's ``lsp-read`` rule puts it in Class A. None is offered
to the model unless a server is configured.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..lsp import LspClient, LspError, LspStartFailed, Target, default_pool, relative, target_for
from ..lsp.positions import find_column, from_server_column, to_server_column, uri_path
from .dispatch import ToolDispatcher
from .registry import Tool, ToolRegistry
from .results import HandlerRefusal

LSP_DEFINITION = "lsp_definition"
LSP_REFERENCES = "lsp_references"
LSP_SYMBOLS = "lsp_symbols"
LSP_TOOL_NAMES = frozenset({LSP_DEFINITION, LSP_REFERENCES, LSP_SYMBOLS})

MAX_LOCATIONS = 50
MAX_SYMBOLS = 200
_SNIPPET_CHARS = 160

_POSITION_PROPS: dict[str, Any] = {
    "path": {"type": "string", "description": "Workspace-relative or absolute path of the file"},
    "line": {"type": "integer", "minimum": 1, "description": "1-based line the symbol is on"},
    "symbol": {
        "type": "string",
        "description": "The name on that line to look up, e.g. 'parse_date'. Easier than column.",
    },
    "column": {
        "type": "integer",
        "minimum": 1,
        "description": "1-based column of the symbol; only when symbol is not enough",
    },
}

_TOOLS = (
    Tool(
        name=LSP_DEFINITION,
        description=(
            "Find where a symbol is defined, using the language server. Give the file, the "
            "1-based line the symbol appears on, and the symbol's name. Returns path:line:col "
            "and the line's text."
        ),
        parameters={
            "type": "object",
            "properties": dict(_POSITION_PROPS),
            "required": ["path", "line"],
            "additionalProperties": False,
        },
        side_effect_class="auto",
        parallel_safe=True,
        path_fields=("path",),
    ),
    Tool(
        name=LSP_REFERENCES,
        description=(
            "Find every use of a symbol across the workspace, using the language server. Give "
            "the file, the 1-based line the symbol appears on, and its name. Returns up to "
            f"{MAX_LOCATIONS} path:line:col locations with each line's text."
        ),
        parameters={
            "type": "object",
            "properties": {
                **_POSITION_PROPS,
                "include_declaration": {
                    "type": "boolean",
                    "description": "Also list the definition itself (default true)",
                },
            },
            "required": ["path", "line"],
            "additionalProperties": False,
        },
        side_effect_class="auto",
        parallel_safe=True,
        path_fields=("path",),
    ),
    Tool(
        name=LSP_SYMBOLS,
        description=(
            "List the classes, functions and other symbols a file declares, with their lines, "
            "using the language server. With query, search the whole workspace for symbols "
            "whose name matches instead; path then only picks the language."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": _POSITION_PROPS["path"],
                "query": {
                    "type": "string",
                    "description": "Name to search for across the workspace; empty lists the file",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        side_effect_class="auto",
        parallel_safe=True,
        path_fields=("path",),
    ),
)


def register_lsp_tools(registry: ToolRegistry) -> None:
    """Register the three tools. ``create_registry`` calls this only with servers."""
    for tool in _TOOLS:
        registry.register(tool)


def register_lsp_handlers(dispatcher: ToolDispatcher) -> None:
    """Bind the handlers of the tools the registry offers.

    With no server configured the tools are not registered, so there is
    nothing to bind. A file no server matches is refused by the handler.
    """
    for name, handler in (
        (LSP_DEFINITION, lsp_definition),
        (LSP_REFERENCES, lsp_references),
        (LSP_SYMBOLS, lsp_symbols),
    ):
        if dispatcher.registry.get(name) is not None:
            dispatcher.register_handler(name, handler)


def _lines(text: str) -> list[str]:
    return text.splitlines() or [""]


# ── shared plumbing ───────────────────────────────────────────────────


async def _target(session: object, path: str, tool: str) -> Target:
    target = await target_for(session, Path(path))
    if target is None:
        raise HandlerRefusal(
            "lsp_unavailable",
            f"No language server is configured for {path} (lsp.servers in config.yaml), so "
            f"{tool} cannot run on it. Use fs_grep to find the symbol instead.",
        )
    return target


async def _client(target: Target) -> LspClient:
    try:
        return await default_pool().client(target.config, target.server, target.root)
    except LspStartFailed as e:
        raise HandlerRefusal(
            "lsp_unavailable",
            f"The language server `{target.server.label}` could not start: {e}. "
            "Use fs_grep instead.",
        ) from e


async def _open(client: LspClient, target: Target) -> list[str]:
    """Send the file's current text; returns its lines."""
    try:
        text = await asyncio.to_thread(target.path.read_text, encoding="utf-8", errors="replace")
    except OSError as e:
        raise HandlerRefusal("lsp_unreadable", f"Cannot read {target.rel}: {e}") from e
    try:
        await client.sync(target.path, text)
    except LspError as e:
        raise HandlerRefusal("lsp_failed", f"The language server stopped: {e}") from e
    return _lines(text)


def _position(lines: list[str], line: int, symbol: str, column: int, encoding: str) -> Any:
    if line < 1 or line > len(lines):
        raise HandlerRefusal(
            "lsp_bad_position", f"line {line} is outside the file (it has {len(lines)} lines)."
        )
    text = lines[line - 1]
    if column and column > 0:
        col = min(column - 1, len(text))
    elif symbol:
        found = find_column(text, symbol)
        if found is None:
            raise HandlerRefusal(
                "lsp_bad_position",
                f"{symbol!r} is not on line {line}. That line reads: {text.strip()[:120]!r}",
            )
        col = found
    else:
        col = len(text) - len(text.lstrip())
    return {"line": line - 1, "character": to_server_column(text, col, encoding)}


async def _ask(client: LspClient, method: str, params: dict[str, Any]) -> Any:
    try:
        return await client.request(method, params)
    except LspError as e:
        raise HandlerRefusal("lsp_failed", f"The language server could not answer: {e}") from e


def _locations(result: Any) -> list[tuple[str, dict[str, Any]]]:
    """``(uri, range)`` pairs from a Location, Location[] or LocationLink[]."""
    items = result if isinstance(result, list) else [result] if isinstance(result, dict) else []
    out: list[tuple[str, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri") or item.get("targetUri")
        rng = item.get("range") or item.get("targetSelectionRange") or item.get("targetRange")
        if isinstance(uri, str) and isinstance(rng, dict):
            out.append((uri, rng))
    return out


async def _render_locations(
    pairs: list[tuple[str, dict[str, Any]]], target: Target, encoding: str
) -> list[str]:
    cache: dict[Path, list[str] | None] = {}

    def read(path: Path) -> list[str] | None:
        try:
            return _lines(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return None

    rendered: list[str] = []
    for uri, rng in pairs[:MAX_LOCATIONS]:
        start = rng.get("start") or {}
        line0 = int(start.get("line", 0))
        char = int(start.get("character", 0))
        path = uri_path(uri)
        if path is None:
            rendered.append(f"- {uri}:{line0 + 1}")
            continue
        resolved = await asyncio.to_thread(path.resolve)
        rel = relative(resolved, target.root)
        if rel is None:
            # Outside the wall: the path only, never its text.
            rendered.append(f"- {resolved}:{line0 + 1}:{char + 1} (outside the workspace)")
            continue
        if resolved not in cache:
            cache[resolved] = await asyncio.to_thread(read, resolved)
        lines = cache[resolved]
        if lines is None or line0 >= len(lines):
            rendered.append(f"- {rel}:{line0 + 1}:{char + 1}")
            continue
        text = lines[line0]
        col = from_server_column(text, char, encoding) + 1
        rendered.append(f"- {rel}:{line0 + 1}:{col}: {text.strip()[:_SNIPPET_CHARS]}")
    if len(pairs) > MAX_LOCATIONS:
        rendered.append(f"... and {len(pairs) - MAX_LOCATIONS} more")
    return rendered


# ── handlers ──────────────────────────────────────────────────────────


async def lsp_definition(
    *,
    session: object = None,
    tool_call_id: str = "",
    path: str,
    line: int,
    symbol: str = "",
    column: int = 0,
) -> str:
    """Where the symbol at *line* is defined."""
    target = await _target(session, path, LSP_DEFINITION)
    client = await _client(target)
    lines = await _open(client, target)
    pos = _position(lines, line, symbol, column, client.position_encoding)
    result = await _ask(
        client,
        "textDocument/definition",
        {"textDocument": {"uri": target.path.as_uri()}, "position": pos},
    )
    pairs = _locations(result)
    what = symbol.strip() or f"line {line}"
    if not pairs:
        return f"The language server found no definition for {what} in {target.rel}."
    found = await _render_locations(pairs, target, client.position_encoding)
    return f"Definition of {what}:\n" + "\n".join(found)


async def lsp_references(
    *,
    session: object = None,
    tool_call_id: str = "",
    path: str,
    line: int,
    symbol: str = "",
    column: int = 0,
    include_declaration: bool = True,
) -> str:
    """Every use of the symbol at *line*."""
    target = await _target(session, path, LSP_REFERENCES)
    client = await _client(target)
    lines = await _open(client, target)
    pos = _position(lines, line, symbol, column, client.position_encoding)
    result = await _ask(
        client,
        "textDocument/references",
        {
            "textDocument": {"uri": target.path.as_uri()},
            "position": pos,
            "context": {"includeDeclaration": bool(include_declaration)},
        },
    )
    pairs = _locations(result)
    what = symbol.strip() or f"line {line}"
    if not pairs:
        return f"The language server found no references to {what}."
    found = await _render_locations(pairs, target, client.position_encoding)
    return f"{len(pairs)} reference(s) to {what}:\n" + "\n".join(found)


_KINDS = {
    1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method",
    7: "property", 8: "field", 9: "constructor", 10: "enum", 11: "interface",
    12: "function", 13: "variable", 14: "constant", 15: "string", 16: "number",
    17: "boolean", 18: "array", 19: "object", 20: "key", 21: "null", 22: "enum member",
    23: "struct", 24: "event", 25: "operator", 26: "type parameter",
}  # fmt: skip


def _symbol_lines(items: list[Any], depth: int, out: list[str]) -> None:
    for item in items:
        if not isinstance(item, dict) or len(out) >= MAX_SYMBOLS:
            continue
        kind = _KINDS.get(int(item.get("kind") or 0), "symbol")
        rng = item.get("selectionRange") or item.get("range")
        if rng is None:
            rng = (item.get("location") or {}).get("range")
        line = int(((rng or {}).get("start") or {}).get("line", 0)) + 1
        detail = item.get("detail")
        extra = f" {' '.join(str(detail).split())[:80]}" if detail else ""
        out.append(f"{'  ' * depth}- {kind} {item.get('name', '?')}{extra} (line {line})")
        children = item.get("children")
        if isinstance(children, list):
            _symbol_lines(children, depth + 1, out)


async def lsp_symbols(
    *,
    session: object = None,
    tool_call_id: str = "",
    path: str,
    query: str = "",
) -> str:
    """The file's symbols, or the workspace's matching *query*."""
    target = await _target(session, path, LSP_SYMBOLS)
    client = await _client(target)
    if query.strip():
        await _open(client, target)
        result = await _ask(client, "workspace/symbol", {"query": query.strip()})
        items = result if isinstance(result, list) else []
        pairs: list[tuple[str, dict[str, Any]]] = []
        names: list[str] = []
        for item in items:
            loc = item.get("location") if isinstance(item, dict) else None
            if isinstance(loc, dict) and isinstance(loc.get("uri"), str):
                rng = loc.get("range") or {"start": {"line": 0, "character": 0}}
                pairs.append((loc["uri"], rng))
                kind = _KINDS.get(int(item.get("kind") or 0), "symbol")
                names.append(f"{kind} {item.get('name', '?')}")
        if not pairs:
            return f"The language server found no symbol matching {query.strip()!r}."
        found = await _render_locations(pairs, target, client.position_encoding)
        rows = [f"{n} {loc[2:]}" for n, loc in zip(names, found, strict=False)]
        rows.extend(found[len(names) :])
        return f"Symbols matching {query.strip()!r}:\n" + "\n".join(f"- {r}" for r in rows)
    await _open(client, target)
    result = await _ask(
        client, "textDocument/documentSymbol", {"textDocument": {"uri": target.path.as_uri()}}
    )
    out: list[str] = []
    _symbol_lines(result if isinstance(result, list) else [], 0, out)
    if not out:
        return f"The language server lists no symbols in {target.rel}."
    return f"Symbols in {target.rel}:\n" + "\n".join(out)
