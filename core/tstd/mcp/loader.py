"""Load listed MCP servers into a tool registry (TD-4401).

A dead or misconfigured server is a status row, never an exception
out of the daemon. Builtins stay registered either way.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Literal

from ..config import McpConfig, McpServerConfig
from ..logging import get_logger
from ..tools.dispatch import ToolDispatcher
from ..tools.registry import ToolRegistry
from .attach import (
    PROGRESS_EVENT_INTERVAL,
    AttachedSessions,
    McpClient,
    _handler,
    register_server_tools,
    replace_server_tools,
)
from .errors import McpError
from .http import HttpMcpClient
from .inject import server_values
from .jsonrpc import (
    CALL_TIMEOUT,
    HANDSHAKE_TIMEOUT,
    MAX_CALL_SECONDS,
    ExcludedTool,
    McpRemoteTool,
    sanitize_remote_tools,
)
from .stdio import StdioMcpClient
from .urls import assert_http_url

__all__ = ["PROGRESS_EVENT_INTERVAL", "McpServerStatus", "McpSupervisor", "_handler"]

log = get_logger("tstd.mcp")

DoctorStatus = Literal["ok", "fail", "skip"]


@dataclass(frozen=True)
class McpServerStatus:
    """Doctor row for one configured server."""

    server_id: str
    status: DoctorStatus
    detail: str
    fix: str | None = None
    #: Advertised tools left out of the registry, with reasons (TD-5104).
    excluded: tuple[ExcludedTool, ...] = ()


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
        f"Set mcp.servers.{server_id}.url to a listening http(s) endpoint that "
        "speaks MCP (loopback, or https with allow_remote: true)."
    )


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
        self._attached = AttachedSessions()
        self._refreshing: dict[str, asyncio.Task[None]] = {}
        self._refresh_again: set[str] = set()

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
        """Register live MCP tools and their dispatcher handlers.

        The pair is remembered (weakly), so a server's
        ``tools/list_changed`` reaches this session too (TD-5136).
        """
        for live in self._live.values():
            register_server_tools(
                registry,
                dispatcher,
                live.server_id,
                live.client,
                live.remote_tools,
                self._read_only(live.server_id),
            )
        self._attached.add(registry, dispatcher)

    def _read_only(self, server_id: str) -> frozenset[str]:
        spec = self._config.servers.get(server_id)
        return frozenset(spec.read_only_tools) if spec is not None else frozenset()

    def _tools_changed(self, server_id: str) -> None:
        """The server said ``tools/list_changed``: list again, off the reader."""
        if server_id in self._refreshing:
            self._refresh_again.add(server_id)
            return
        task = asyncio.get_running_loop().create_task(self._refresh(server_id))
        self._refreshing[server_id] = task

    async def _refresh(self, server_id: str) -> None:
        """Re-list *server_id*'s tools into its row and every live session."""
        try:
            while True:
                self._refresh_again.discard(server_id)
                live = self._live.get(server_id)
                if live is None:
                    return
                try:
                    listed = await live.client.list_tools()
                except Exception as e:
                    log.warning(
                        "mcp tool list refresh failed",
                        extra={"extra_fields": {"server_id": server_id, "error": str(e)}},
                    )
                    return
                if self._live.get(server_id) is not live:
                    return  # reloaded meanwhile
                tools, excluded = sanitize_remote_tools(server_id, listed)
                live.remote_tools = tools
                self._set_row(server_id, live, tools, [*live.client.excluded, *excluded])
                for registry, dispatcher in self._attached.live():
                    replace_server_tools(
                        registry,
                        dispatcher,
                        server_id,
                        live.client,
                        tools,
                        self._read_only(server_id),
                    )
                log.info(
                    "mcp tool list refreshed",
                    extra={"extra_fields": {"server_id": server_id, "tools": len(tools)}},
                )
                if server_id not in self._refresh_again:
                    return
        finally:
            self._refreshing.pop(server_id, None)

    def _set_row(
        self,
        server_id: str,
        live: _LiveServer,
        tools: list[McpRemoteTool],
        excluded: list[ExcludedTool],
    ) -> None:
        row = _ok_row(server_id, live.client, tools, excluded)
        self._statuses = [row if s.server_id == server_id else s for s in self._statuses]

    async def reload(self, config: McpConfig) -> list[McpServerStatus]:
        """Drop live clients and handshake a new listing. Never raises.

        Live sessions keep the tool set they attached with, and no longer
        follow ``tools/list_changed``; the next session (and doctor) see
        this list. Hot-attach is out of scope.
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
        for task in list(self._refreshing.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._refreshing.clear()
        self._attached.clear()
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
            # Read now, for this child only (TD-5110).
            env = await server_values(server_id, spec)
            client = StdioMcpClient(
                spec.command,
                timeout=spec.timeout_seconds or self._call_timeout,
                max_call_seconds=spec.max_call_seconds or MAX_CALL_SECONDS,
                startup_timeout=self._startup_timeout(spec),
                env=env,
            )
            return await self._handshake(server_id, client, spec)
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
                    fix=f"Set mcp.servers.{server_id}.url to the server's http(s) endpoint.",
                ),
                None,
            )
        try:
            assert_http_url(spec.url, allow_remote=spec.allow_remote)
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
            # Read now, for this server's requests only (TD-5110).
            headers = await server_values(server_id, spec)
            client = HttpMcpClient(
                spec.url,
                timeout=spec.timeout_seconds or self._call_timeout,
                max_call_seconds=spec.max_call_seconds or MAX_CALL_SECONDS,
                startup_timeout=self._startup_timeout(spec),
                headers=headers,
                allow_remote=spec.allow_remote,
            )
            return await self._handshake(server_id, client, spec)
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

    def _startup_timeout(self, spec: McpServerConfig) -> float:
        return spec.startup_timeout_seconds or self._handshake_timeout

    async def _handshake(
        self, server_id: str, client: McpClient, spec: McpServerConfig
    ) -> tuple[McpServerStatus, _LiveServer]:
        # The clients also give each handshake request this long, so a
        # startup timeout above the call timeout is honoured (TD-5105).
        # ``start`` may probe for the era first (TD-5136).
        startup = self._startup_timeout(spec)
        client.on_tools_changed = lambda: self._tools_changed(server_id)
        await asyncio.wait_for(client.start(), timeout=client.startup_seconds)
        listed = await asyncio.wait_for(client.list_tools(), timeout=startup)
        tools, excluded = sanitize_remote_tools(server_id, listed)
        excluded = [*client.excluded, *excluded]
        for item in excluded:
            log.warning(
                "mcp tool excluded",
                extra={
                    "extra_fields": {
                        "server_id": server_id,
                        "tool": item.name,
                        "reason": item.reason,
                    }
                },
            )
        return (
            _ok_row(server_id, client, tools, excluded),
            _LiveServer(server_id=server_id, client=client, remote_tools=tools),
        )


def _ok_row(
    server_id: str, client: McpClient, tools: list[McpRemoteTool], excluded: list[ExcludedTool]
) -> McpServerStatus:
    """The doctor row for a live server: the protocol it speaks and its tools."""
    negotiated = getattr(client, "negotiated", None)
    version = getattr(negotiated, "version", None)
    detail = f"{len(tools)} tool(s) listed"
    if isinstance(version, str):
        detail = f"MCP {version}: {detail}"
    fix = None
    if excluded:
        names = "; ".join(f"{item.name} ({item.reason})" for item in excluded)
        detail += f"; {len(excluded)} excluded: {names}"
        fix = (
            "The other tools work. An excluded tool stays out until the server "
            "advertises a usable name and input schema."
        )
    return McpServerStatus(
        server_id=server_id, status="ok", detail=detail, fix=fix, excluded=tuple(excluded)
    )
