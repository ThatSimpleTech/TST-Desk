"""Load listed MCP servers into a tool registry (TD-4401).

A dead or misconfigured server is a status row, never an exception
out of the daemon. Builtins stay registered either way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..config import McpConfig, McpServerConfig
from ..logging import get_logger
from ..tools.dispatch import ToolDispatcher
from ..tools.registry import Tool, ToolRegistry
from .errors import McpError
from .http import HttpMcpClient, assert_loopback_http_url
from .jsonrpc import (
    CALL_TIMEOUT,
    HANDSHAKE_TIMEOUT,
    McpRemoteTool,
    format_call_result,
    local_tool_name,
    tool_provenance,
)
from .stdio import StdioMcpClient

log = get_logger("tstd.mcp")

DoctorStatus = Literal["ok", "fail", "skip"]


class McpClient(Protocol):
    async def start(self) -> None: ...
    async def list_tools(self) -> list[McpRemoteTool]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any: ...
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class McpServerStatus:
    """Doctor row for one configured server."""

    server_id: str
    status: DoctorStatus
    detail: str
    fix: str | None = None


@dataclass
class _LiveServer:
    server_id: str
    client: McpClient
    remote_tools: list[McpRemoteTool]


def _stdio_fix(server_id: str) -> str:
    return (
        f"Check that mcp.servers.{server_id}.command is a working argv "
        "and the process speaks MCP JSON-RPC on stdio."
    )


def _http_fix(server_id: str) -> str:
    return (
        f"Set mcp.servers.{server_id}.url to a listening loopback http(s) "
        "endpoint that speaks MCP JSON-RPC."
    )


def _handler(client: McpClient, remote_name: str) -> Callable[..., Awaitable[str]]:
    async def call(*, session: object = None, tool_call_id: str = "", **arguments: Any) -> str:
        del session, tool_call_id
        result = await client.call_tool(remote_name, arguments)
        return format_call_result(result)

    return call


class McpSupervisor:
    """Owns configured MCP clients for the daemon lifetime.

    ``ensure_loaded`` is idempotent and never raises. ``attach`` registers
    tools plus ``ToolDispatcher`` handlers so every call still hits the
    classifier chokepoint.
    """

    def __init__(
        self,
        config: McpConfig,
        *,
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
        call_timeout: float = CALL_TIMEOUT,
    ) -> None:
        self._config = config
        self._handshake_timeout = handshake_timeout
        self._call_timeout = call_timeout
        self._lock = asyncio.Lock()
        self._loaded = False
        self._live: dict[str, _LiveServer] = {}
        self._statuses: list[McpServerStatus] = []

    def doctor_rows(self) -> list[McpServerStatus]:
        """Last load statuses, empty when ``mcp.servers`` is empty."""
        return list(self._statuses)

    async def ensure_loaded(self) -> list[McpServerStatus]:
        """Handshake each listed server. Never raises."""
        async with self._lock:
            if not self._loaded:
                await self._connect_all()
                self._loaded = True
            return list(self._statuses)

    def attach(self, registry: ToolRegistry, dispatcher: ToolDispatcher) -> None:
        """Register live MCP tools and their dispatcher handlers."""
        for live in self._live.values():
            for remote in live.remote_tools:
                local = local_tool_name(live.server_id, remote.name)
                registry.register(
                    Tool(
                        name=local,
                        description=remote.description,
                        parameters=remote.parameters,
                        side_effect_class="ask",
                        parallel_safe=True,
                        provenance=tool_provenance(live.server_id),
                        # TD-4402: do not infer path/host keys from the
                        # remote schema. Undeclared fields fail toward B.
                        path_fields=(),
                        host_fields=(),
                    )
                )
                dispatcher.register_handler(local, _handler(live.client, remote.name))

    async def reload(self, config: McpConfig) -> list[McpServerStatus]:
        """Drop live clients and handshake a new listing. Never raises.

        Live sessions keep the tool set they attached with; the next
        session (and doctor) see this list. Hot-attach is out of scope.
        """
        async with self._lock:
            await self._reset_unlocked()
            self._config = config
            await self._connect_all()
            self._loaded = True
            return list(self._statuses)

    async def aclose(self) -> None:
        async with self._lock:
            await self._reset_unlocked()
            self._loaded = False

    async def _reset_unlocked(self) -> None:
        for live in self._live.values():
            try:
                await live.client.aclose()
            except Exception:
                log.exception(
                    "mcp client close failed",
                    extra={"extra_fields": {"server_id": live.server_id}},
                )
        self._live.clear()
        self._statuses = []

    async def _connect_all(self) -> None:
        self._live = {}
        self._statuses = []
        for server_id in sorted(self._config.servers):
            spec = self._config.servers[server_id]
            status, live = await self._connect_one(server_id, spec)
            self._statuses.append(status)
            if live is not None:
                self._live[server_id] = live

    async def _connect_one(
        self, server_id: str, spec: McpServerConfig
    ) -> tuple[McpServerStatus, _LiveServer | None]:
        if not spec.enabled:
            return (
                McpServerStatus(
                    server_id=server_id,
                    status="skip",
                    detail="disabled in config",
                ),
                None,
            )
        if spec.transport == "stdio":
            return await self._connect_stdio(server_id, spec)
        return await self._connect_http(server_id, spec)

    async def _connect_stdio(
        self, server_id: str, spec: McpServerConfig
    ) -> tuple[McpServerStatus, _LiveServer | None]:
        if not spec.command:
            return (
                McpServerStatus(
                    server_id=server_id,
                    status="fail",
                    detail="stdio server has no command",
                    fix=f"Set mcp.servers.{server_id}.command to a non-empty argv.",
                ),
                None,
            )
        client: StdioMcpClient | None = None
        try:
            client = StdioMcpClient(spec.command, timeout=self._call_timeout)
            return await self._handshake(server_id, client)
        except Exception as e:
            if client is not None:
                await client.aclose()
            detail = str(e) or e.__class__.__name__
            log.warning(
                "mcp stdio server failed",
                extra={"extra_fields": {"server_id": server_id, "error": detail}},
            )
            return (
                McpServerStatus(
                    server_id=server_id,
                    status="fail",
                    detail=detail,
                    fix=e.fix if isinstance(e, McpError) and e.fix else _stdio_fix(server_id),
                ),
                None,
            )

    async def _connect_http(
        self, server_id: str, spec: McpServerConfig
    ) -> tuple[McpServerStatus, _LiveServer | None]:
        if not spec.url:
            return (
                McpServerStatus(
                    server_id=server_id,
                    status="fail",
                    detail="http server has no url",
                    fix=f"Set mcp.servers.{server_id}.url to a loopback http(s) URL.",
                ),
                None,
            )
        try:
            assert_loopback_http_url(spec.url)
        except McpError as e:
            log.warning(
                "mcp http url refused",
                extra={"extra_fields": {"server_id": server_id, "error": str(e)}},
            )
            return (
                McpServerStatus(
                    server_id=server_id,
                    status="fail",
                    detail=str(e),
                    fix=e.fix or _http_fix(server_id),
                ),
                None,
            )
        client: HttpMcpClient | None = None
        try:
            client = HttpMcpClient(spec.url, timeout=self._call_timeout)
            return await self._handshake(server_id, client)
        except Exception as e:
            if client is not None:
                await client.aclose()
            detail = str(e) or e.__class__.__name__
            log.warning(
                "mcp http server failed",
                extra={"extra_fields": {"server_id": server_id, "error": detail}},
            )
            return (
                McpServerStatus(
                    server_id=server_id,
                    status="fail",
                    detail=detail,
                    fix=e.fix if isinstance(e, McpError) and e.fix else _http_fix(server_id),
                ),
                None,
            )

    async def _handshake(
        self, server_id: str, client: McpClient
    ) -> tuple[McpServerStatus, _LiveServer]:
        await asyncio.wait_for(client.start(), timeout=self._handshake_timeout)
        tools = await asyncio.wait_for(client.list_tools(), timeout=self._handshake_timeout)
        return (
            McpServerStatus(
                server_id=server_id,
                status="ok",
                detail=f"{len(tools)} tool(s) listed",
            ),
            _LiveServer(server_id=server_id, client=client, remote_tools=tools),
        )
