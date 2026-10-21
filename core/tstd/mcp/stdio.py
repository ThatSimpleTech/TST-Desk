"""Newline-delimited JSON-RPC client for a user-listed stdio MCP server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Mapping
from typing import Any

from ..logging import get_logger
from .calls import (
    CallTimedOut,
    ToolsChanged,
    checked_result,
    is_tools_changed,
    list_all_tools,
    reply_to_server_request,
    tool_call_params,
    watch_call,
)
from .era import PROBE_TIMEOUT, Negotiated, negotiate, rpc_error
from .errors import McpError
from .jsonrpc import (
    CALL_TIMEOUT,
    MAX_CALL_SECONDS,
    STREAM_LIMIT,
    ExcludedTool,
    McpRemoteTool,
    ProgressCallback,
    cancelled_notification,
    progress_update,
)

log = get_logger("tstd.mcp.stdio")


class StdioMcpClient:
    """One JSON-RPC channel over a child process's stdio.

    A reader task owns stdout: it matches responses to requests by id,
    feeds progress to the call that owns the token, answers a legacy
    server's ``ping``, and reports ``notifications/tools/list_changed`` to
    ``on_tools_changed`` (TD-5136). Calls may overlap.

    Stderr is drained so a chatty server cannot fill the pipe and stall.
    The child inherits the daemon environment plus ``env``, this server's
    own values (TD-5110). ``env`` goes to this child's spawn and nowhere
    else: never ``os.environ``, so no other child sees it.
    """

    def __init__(
        self,
        command: list[str],
        *,
        timeout: float = CALL_TIMEOUT,
        max_call_seconds: float = MAX_CALL_SECONDS,
        startup_timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not command:
            raise McpError("stdio MCP command must be a non-empty argv")
        self._command = command
        self._env = dict(env or {})
        self._timeout = timeout
        self._max_call = max(max_call_seconds, timeout)
        # The handshake (probe, initialize, tools/list) answers in its own
        # time, which may be longer than a call's (TD-5105).
        self._startup_timeout = startup_timeout or timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._progress: dict[str, tuple[asyncio.Event, ProgressCallback | None]] = {}
        self._start_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._listen_id: int | None = None
        self.negotiated: Negotiated | None = None
        #: stdio refuses no advertised tool itself; the HTTP client may.
        self.excluded: list[ExcludedTool] = []
        #: Set by the supervisor; called when the server's tools change.
        self.on_tools_changed: ToolsChanged | None = None

    async def start(self) -> None:
        async with self._start_lock:
            if self._proc is not None:
                return
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self._command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=STREAM_LIMIT,
                    env={**os.environ, **self._env} if self._env else None,
                )
            except OSError as e:
                raise McpError(f"failed to start stdio MCP: {e}") from e
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            self._reader_task = asyncio.create_task(self._read_stdout())
            startup = self._startup_timeout
            negotiated = await negotiate(
                self._raw_request,
                probe_timeout=min(PROBE_TIMEOUT, startup),
                startup_timeout=startup,
                timeout_means_legacy=True,
            )
            self.negotiated = negotiated
            if negotiated.era == "legacy":
                await self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            elif negotiated.tools_list_changed:
                self._listen()

    @property
    def startup_seconds(self) -> float:
        """Longest :meth:`start` may take: the probe, then the handshake."""
        return min(PROBE_TIMEOUT, self._startup_timeout) + self._startup_timeout

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        wait_seconds: float | None = None,
    ) -> Any:
        await self.start()
        params = self.negotiated.stamp(params) if self.negotiated else params
        return checked_result(
            await self._raw_request(method, params, wait_seconds or self._timeout)
        )

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.start()
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    async def list_tools(self) -> list[McpRemoteTool]:
        """``tools/list``, every page, with the startup timeout."""
        startup = self._startup_timeout
        return await list_all_tools(
            lambda params: self.request("tools/list", params, wait_seconds=startup)
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> Any:
        """``tools/call`` with a progress token (TD-5105).

        The call may run as long as the server keeps reporting progress, up
        to the hard cap; silence for ``timeout`` seconds ends it.  A call
        that times out, or whose task is cancelled (the user stopped the
        turn), sends ``notifications/cancelled`` so the server can stop too.
        """
        await self.start()
        req_id = self._take_id()
        token = f"tstd-{req_id}"
        params = tool_call_params(name, arguments or {}, token)
        if self.negotiated is not None:
            params = self.negotiated.stamp(params) or params
        progressed = asyncio.Event()
        self._progress[token] = (progressed, on_progress)
        future = self._expect(req_id)
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": params}
            )
            result = await watch_call(future, progressed, idle=self._timeout, hard=self._max_call)
        except CallTimedOut as e:
            self._send_cancelled(req_id, str(e))
            raise McpError(f"MCP tool {name!r} cancelled: {e}") from None
        except asyncio.CancelledError:
            self._send_cancelled(req_id, "the user stopped the turn")
            raise
        finally:
            self._progress.pop(token, None)
            self._pending.pop(req_id, None)
        return checked_result(result)

    def _take_id(self) -> int:
        req_id = self._next_id
        self._next_id += 1
        return req_id

    def _expect(self, req_id: int) -> asyncio.Future[Any]:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        return future

    async def _raw_request(self, method: str, params: dict[str, Any] | None, wait: float) -> Any:
        """One request, unstamped; the era probe uses it before a version is set."""
        req_id = self._take_id()
        future = self._expect(req_id)
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._write(message)
            return await asyncio.wait_for(future, timeout=wait)
        finally:
            self._pending.pop(req_id, None)

    def _listen(self) -> None:
        """Ask a modern server for ``tools/list_changed`` (``subscriptions/listen``).

        The request stays open for the life of the process; its
        notifications arrive on stdout like any other.
        """
        assert self.negotiated is not None
        req_id = self._take_id()
        self._listen_id = req_id
        params = self.negotiated.stamp({"notifications": {"toolsListChanged": True}})
        self._write_nowait(
            {"jsonrpc": "2.0", "id": req_id, "method": "subscriptions/listen", "params": params}
        )

    async def _write(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            raise McpError("stdio MCP server is not running")
        proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        try:
            await proc.stdin.drain()
        except (ConnectionError, OSError) as e:
            raise McpError(f"stdio MCP server stopped reading: {e}") from e

    def _write_nowait(self, message: dict[str, Any]) -> None:
        """Best effort, without awaiting: the caller may be cancelled."""
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            return
        try:
            proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        except (OSError, RuntimeError):
            log.debug("could not write to stdio MCP server", exc_info=True)

    def _send_cancelled(self, req_id: int, reason: str) -> None:
        self._write_nowait(cancelled_notification(req_id, reason))

    async def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                try:
                    parsed: Any = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(parsed, dict):
                    await self._on_message(parsed)
        except (ConnectionError, OSError, ValueError):
            log.debug("stdio MCP stdout read failed", exc_info=True)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(McpError("stdio MCP server closed stdout"))

    async def _on_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method is None:
            req_id = message.get("id")
            future = self._pending.get(req_id) if isinstance(req_id, int) else None
            if future is None or future.done():
                return
            if "error" in message:
                future.set_exception(rpc_error(message["error"]))
            else:
                future.set_result(message.get("result"))
            return
        if "id" in message:
            self._write_nowait(reply_to_server_request(message))
            return
        if is_tools_changed(message):
            if self.on_tools_changed is not None:
                self.on_tools_changed()
            return
        params = message.get("params")
        token = params.get("progressToken") if isinstance(params, dict) else None
        if not isinstance(token, str) or token not in self._progress:
            return
        update = progress_update(message, token)
        if update is None:
            return
        progressed, on_progress = self._progress[token]
        progressed.set()
        if on_progress is not None:
            try:
                await on_progress(*update)
            except Exception:
                log.exception("mcp progress callback failed")

    async def aclose(self) -> None:
        proc = self._proc
        self._proc = None
        for task in (self._stderr_task, self._reader_task):
            if task is not None:
                task.cancel()
        self._stderr_task = self._reader_task = None
        if proc is None:
            return
        if proc.returncode is None:
            # Closing stdin is the spec's shutdown signal; then terminate.
            if proc.stdin is not None:
                with contextlib.suppress(OSError, RuntimeError):
                    proc.stdin.close()
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()

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
