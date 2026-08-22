"""Load MCP servers from config and register their tools (TD-4401).

One manager per daemon, built from ``ModelConfig.mcp``. Servers start
lazily on the first session attach (or doctor run) and stay up until
daemon shutdown or a settings edit replaces them (TD-4403) — the same
lifecycle as the desktop drivers. A server that fails to start, or
dies later, is recorded as a failure and surfaces as a doctor row; it
never takes the daemon down.

Two transports, both named entirely by config: stdio (a child process
spawned from ``command``) and loopback HTTP (``url``, validated as
on-box at load). The HTTP transport speaks the streamable-HTTP MCP
transport in its JSON response mode only — a server that insists on an
event stream fails with a clear message rather than a half-read.

Tool names are prefixed ``mcp__<server>__<tool>`` so a server can
never shadow a builtin, and each registered ``Tool`` carries
``source="mcp:<server>"`` as provenance. Provenance is metadata: the
classifier (TD-702) gates every call, MCP or not.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from ..config import McpConfig, McpServerConfig
from ..desktop.protocol import DesktopError
from ..desktop.stdio_mcp import StdioMcpClient
from ..logging import get_logger
from ..tools.dispatch import ToolDispatcher
from ..tools.registry import Tool, ToolRegistry

log = get_logger("tstd.mcp")

# Per-request timeout, matching the stdio client's budget.
_CALL_TIMEOUT = 30.0

# Argument keys the dispatcher reserves when it invokes a handler; an
# MCP argument with one of these names is dropped from the call (v0.8
# limitation, documented in the architecture guide).
_RESERVED_HANDLER_KEYS = frozenset({"session", "tool_call_id"})


class McpError(Exception):
    """A generic MCP server failure.

    Deliberately not a ``DesktopError``: the dispatcher's computer-use
    handling keys off that type, and a git server's failure is not a
    computer-use one.
    """


@dataclass(frozen=True)
class McpServerStatus:
    """One server's load state, as the doctor and ``mcp_state`` report it."""

    name: str
    transport: Literal["stdio", "http"]
    status: Literal["ready", "failed", "starting", "disabled"]
    detail: str = ""
    tool_count: int = 0


@dataclass(frozen=True)
class McpTool:
    """One tool a server advertised via ``tools/list``."""

    server: str
    name: str
    description: str
    parameters: dict[str, Any]


class McpTransport(Protocol):
    """The slice of an MCP connection the manager actually uses."""

    async def start(self) -> None: ...

    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> Any: ...

    def is_alive(self) -> bool: ...

    async def aclose(self) -> None: ...


def _stdio_error(message: str) -> McpError:
    return McpError(message)


class StdioTransport:
    """Adapts the desktop stdio client to the generic transport shape."""

    def __init__(self, command: list[str]) -> None:
        self._client = StdioMcpClient(
            command,
            error_mapper=_stdio_error,
            transport_code="mcp_error",
            transport_label="MCP server",
        )

    async def start(self) -> None:
        try:
            await self._client.start()
        except DesktopError as e:
            # Transport failure with MCP wording; generic servers never
            # leak computer-use error types past this boundary.
            raise McpError(e.message) from e

    async def list_tools(self) -> list[dict[str, Any]]:
        return await self._client.list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> Any:
        return await self._client.call_tool(name, arguments)

    def is_alive(self) -> bool:
        return self._client.is_alive()

    async def aclose(self) -> None:
        await self._client.aclose()


class HttpTransport:
    """MCP over streamable HTTP, JSON response mode only.

    The URL comes from config and was validated as loopback at load, so
    every destination this opens traces to ``mcp.servers`` (the
    ``test_outbound_hosts`` rule). A server that answers with an event
    stream is refused with a clear message — v0.8 reads JSON.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=_CALL_TIMEOUT)
        try:
            await self._request("initialize", _INITIALIZE_PARAMS)
            await self._notify("notifications/initialized")
        except BaseException:
            await self.aclose()
            raise

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return [tool for tool in tools if isinstance(tool, dict)]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> Any:
        return await self._request("tools/call", {"name": name, "arguments": arguments or {}})

    def is_alive(self) -> bool:
        # No cheap liveness probe for HTTP; report last-known state.
        return True

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        body = await self._post(method, params, with_id=True)
        if "error" in body:
            err = body["error"]
            raise McpError(err.get("message", str(err)) if isinstance(err, dict) else str(err))
        return body.get("result")

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._post(method, params, with_id=False)

    async def _post(
        self, method: str, params: dict[str, Any] | None, *, with_id: bool
    ) -> dict[str, Any]:
        client = self._client
        if client is None:
            raise McpError("MCP HTTP transport is not started")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if with_id:
            message["id"] = 1
        if params is not None:
            message["params"] = params
        headers = {"Accept": "application/json"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            resp = await client.post(self._url, json=message, headers=headers)
        except httpx.HTTPError as e:
            raise McpError(f"MCP HTTP request failed: {e}") from e
        session_id = resp.headers.get("Mcp-Session-Id")
        if session_id:
            self._session_id = session_id
        if resp.status_code >= 400:
            raise McpError(f"MCP HTTP {resp.status_code} for {method}")
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            raise McpError("server answered with an event stream; only JSON is supported")
        if not resp.content:
            return {}
        try:
            parsed: Any = json.loads(resp.content)
        except json.JSONDecodeError as e:
            raise McpError(f"MCP server sent invalid JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise McpError("MCP server sent a non-object JSON response")
        return parsed


_INITIALIZE_PARAMS: dict[str, Any] = {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "tstd", "version": "0.1.0"},
}


def _sanitize_schema(name: str, schema: Any) -> dict[str, Any]:
    """Coerce an advertised input schema into one the registry accepts.

    ``Tool`` requires ``type: "object"`` with a ``properties`` key. A
    server that advertises something else gets a permissive empty
    schema rather than a crash — arguments are still validated against
    whatever we declare, so permissive here means "accept and forward",
    not "skip validation".
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        log.warning(
            "MCP tool advertised a non-object schema; using permissive",
            extra={"extra_fields": {"tool": name}},
        )
        return {"type": "object", "properties": {}}
    return schema


def _content_to_text(result: Any, server: str) -> str:
    """Flatten a ``tools/call`` result into the text the model sees."""
    if not isinstance(result, dict):
        return json.dumps(result) if result is not None else ""
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return json.dumps(result)
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        else:
            parts.append(f"[non-text content block: {block.get('type', 'unknown')}]")
    if result.get("isError"):
        joined = "\n".join(parts)
        raise McpError(f"MCP server {server} reported an error: {joined}")
    return "\n".join(parts)


class McpManager:
    """Owns the configured MCP servers: start once, report, route calls."""

    def __init__(self, config: McpConfig) -> None:
        self._config = config
        self._transports: dict[str, McpTransport] = {}
        self._tools: dict[str, list[McpTool]] = {}
        self._failures: dict[str, str] = {}
        self._started = False
        self._start_lock = asyncio.Lock()

    async def ensure_started(self) -> None:
        """Start every enabled server not already running; never raise.

        A server that fails to start is recorded — the doctor and the
        per-session ``mcp_state`` event report it, and the rest of the
        daemon carries on. That is the AC: a dead server is a doctor
        row, not a dead daemon. Already-running servers are left alone,
        so a config generation change (``reconcile``) starts only what
        the edit added.
        """
        async with self._start_lock:
            if self._started:
                return
            self._started = True
            for name, server in self._config.servers.items():
                if not server.enabled or name in self._transports:
                    continue
                try:
                    transport = self._build_transport(server)
                    await transport.start()
                    advertised = await transport.list_tools()
                except Exception as exc:
                    self._failures[name] = str(exc) or type(exc).__name__
                    log.warning(
                        "MCP server failed to start",
                        extra={"extra_fields": {"server": name, "error": str(exc)}},
                    )
                    continue
                self._transports[name] = transport
                self._tools[name] = [
                    McpTool(
                        server=name,
                        name=str(item.get("name", "")),
                        description=str(item.get("description", "")),
                        parameters=_sanitize_schema(
                            f"{name}::{item.get('name', '')}", item.get("inputSchema")
                        ),
                    )
                    for item in advertised
                    if item.get("name")
                ]

    async def reconcile(self, config: McpConfig) -> None:
        """Adopt a freshly loaded config after a settings edit (TD-4403).

        Servers the edit removed, disabled, or changed are closed and
        dropped — a running process for a server that is no longer
        configured is exactly the kind of zombie this owns. Unchanged
        servers keep their process. The next ``ensure_started`` brings
        new and changed servers up, so the edit reaches new sessions
        without a daemon restart — the same contract a slug edit holds.
        """
        async with self._start_lock:
            stale: list[tuple[str, McpTransport]] = []
            for name, transport in self._transports.items():
                server = config.servers.get(name)
                if server is None or not server.enabled or server != self._config.servers.get(name):
                    stale.append((name, transport))
            for name, transport in stale:
                try:
                    await transport.aclose()
                except Exception as exc:
                    log.warning(
                        "MCP server failed to close cleanly",
                        extra={"extra_fields": {"server": name, "error": str(exc)}},
                    )
                self._transports.pop(name, None)
                self._tools.pop(name, None)
                self._failures.pop(name, None)
            self._config = config
            self._started = False

    def _build_transport(self, server: McpServerConfig) -> McpTransport:
        if server.url:
            return HttpTransport(server.url)
        argv = server.command if isinstance(server.command, list) else [server.command]
        return StdioTransport(argv)

    def has_servers(self) -> bool:
        return bool(self._config.servers)

    def statuses(self) -> list[McpServerStatus]:
        """Per-server state, sorted by name for stable reports.

        Call after ``ensure_started`` — before that, enabled servers
        honestly read ``starting``.
        """
        out: list[McpServerStatus] = []
        for name, server in sorted(self._config.servers.items()):
            kind: Literal["stdio", "http"] = "stdio" if server.command else "http"
            transport = self._transports.get(name)
            if not server.enabled:
                out.append(McpServerStatus(name, kind, "disabled"))
            elif name in self._failures:
                out.append(McpServerStatus(name, kind, "failed", detail=self._failures[name]))
            elif transport is not None and not transport.is_alive():
                out.append(McpServerStatus(name, kind, "failed", detail="server process exited"))
            elif transport is not None:
                out.append(
                    McpServerStatus(name, kind, "ready", tool_count=len(self._tools.get(name, ())))
                )
            else:
                out.append(McpServerStatus(name, kind, "starting"))
        return out

    def tools(self) -> list[McpTool]:
        """Every ready server's tools, flattened, sorted for a stable list."""
        flat = [tool for tools in self._tools.values() for tool in tools]
        return sorted(flat, key=lambda t: (t.server, t.name))

    async def call(self, server: str, tool: str, arguments: dict[str, Any]) -> str:
        """Run one MCP tool call and return the text the model sees."""
        transport = self._transports.get(server)
        if transport is None:
            raise McpError(f"MCP server {server!r} is not ready")
        try:
            result = await asyncio.wait_for(
                transport.call_tool(tool, arguments), timeout=_CALL_TIMEOUT
            )
        except McpError:
            raise
        except DesktopError as e:
            raise McpError(e.message) from e
        except TimeoutError as e:
            raise McpError(f"MCP server {server!r} timed out calling {tool!r}") from e
        return _content_to_text(result, server)

    async def aclose(self) -> None:
        for name, transport in self._transports.items():
            try:
                await transport.aclose()
            except Exception as exc:
                log.warning(
                    "MCP server failed to close cleanly",
                    extra={"extra_fields": {"server": name, "error": str(exc)}},
                )
        self._transports.clear()
        self._tools.clear()


def _make_mcp_handler(manager: McpManager, server: str, tool: str) -> Callable[..., Any]:
    """Build the dispatcher handler for one MCP tool.

    A factory rather than a loop closure so each tool binds its own
    name. The dispatcher splats parsed arguments as keywords on top of
    ``session``/``tool_call_id``; an MCP argument sharing a reserved
    name is dropped rather than misrouted.
    """

    async def handler(**kwargs: Any) -> str:
        arguments = {k: v for k, v in kwargs.items() if k not in _RESERVED_HANDLER_KEYS}
        return await manager.call(server, tool, arguments)

    return handler


def register_mcp_tools(
    registry: ToolRegistry,
    dispatcher: ToolDispatcher,
    manager: McpManager,
) -> int:
    """Register every ready MCP tool into a fresh per-session registry.

    Tools land with conservative classifier metadata — ``ask``, never
    parallel, no path/host claims — and their provenance in ``source``.
    TD-4402 adds the rule that keeps them at Class B or worse; nothing
    here bypasses the chokepoint, because registration is the same
    ``registry.register`` path builtins use.

    Returns the number of tools registered.
    """
    count = 0
    for mcp_tool in manager.tools():
        registry_name = f"mcp__{mcp_tool.server}__{mcp_tool.name}"
        description = mcp_tool.description or (
            f"Tool {mcp_tool.name!r} from MCP server {mcp_tool.server!r}."
        )
        registry.register(
            Tool(
                name=registry_name,
                description=description,
                parameters=mcp_tool.parameters,
                # Conservative by default: an external tool is ask-gated
                # until TD-4402's classifier rules say otherwise.
                side_effect_class="ask",
                parallel_safe=False,
                source=f"mcp:{mcp_tool.server}",
            )
        )
        dispatcher.register_handler(
            registry_name, _make_mcp_handler(manager, mcp_tool.server, mcp_tool.name)
        )
        count += 1
    return count
