"""Newline-delimited JSON-RPC client for a user-listed stdio MCP server."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..logging import get_logger
from .errors import McpError
from .jsonrpc import (
    CALL_TIMEOUT,
    STREAM_LIMIT,
    McpRemoteTool,
    initialize_params,
    jsonrpc_error_text,
    parse_tools_list,
)

log = get_logger("tstd.mcp.stdio")


class StdioMcpClient:
    """One JSON-RPC session over a child process's stdio.

    Stderr is drained so a chatty server cannot fill the pipe and stall.
    No extra ``env`` map — the child inherits the daemon environment.
    """

    def __init__(self, command: list[str], *, timeout: float = CALL_TIMEOUT) -> None:
        if not command:
            raise McpError("stdio MCP command must be a non-empty argv")
        self._command = command
        self._timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=STREAM_LIMIT,
            )
        except OSError as e:
            raise McpError(f"failed to start stdio MCP: {e}") from e
        assert self._proc.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self.request("initialize", initialize_params())
        await self.notify("notifications/initialized")

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        await self.start()
        async with self._lock:
            return await asyncio.wait_for(
                self._exchange(method, params, with_id=True),
                timeout=self._timeout,
            )

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.start()
        async with self._lock:
            await asyncio.wait_for(
                self._exchange(method, params, with_id=False),
                timeout=self._timeout,
            )

    async def list_tools(self) -> list[McpRemoteTool]:
        return parse_tools_list(await self.request("tools/list"))

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return await self.request("tools/call", {"name": name, "arguments": arguments or {}})

    async def aclose(self) -> None:
        proc = self._proc
        self._proc = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        if proc is None:
            return
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()

    async def _exchange(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        with_id: bool,
    ) -> Any:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise McpError("stdio MCP server is not running")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        req_id: int | None = None
        if with_id:
            req_id = self._next_id
            self._next_id += 1
            message["id"] = req_id
        if params is not None:
            message["params"] = params
        proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        if not with_id:
            return None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                raise McpError("stdio MCP server closed stdout")
            try:
                parsed: Any = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict) or parsed.get("id") != req_id:
                continue
            if "error" in parsed:
                raise McpError(jsonrpc_error_text(parsed["error"]))
            return parsed.get("result")

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                log.debug(
                    "mcp stdio stderr: %s",
                    line.decode("utf-8", errors="replace").rstrip(),
                )
        except asyncio.CancelledError:
            return
