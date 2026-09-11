"""Newline-delimited JSON-RPC client for an MCP stdio sidecar (TD-3301).

No socket. The child is spawned with stdin/stdout pipes. Stderr is drained
so a chatty sidecar cannot fill the pipe and stall.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from ..logging import get_logger
from .permissions import (
    is_permission_failure,
    is_secure_desktop_failure,
    is_uipi_failure,
)
from .protocol import DesktopError

log = get_logger("tstd.desktop.mcp")

# MCP handshake the sidecar accepts; a date string, not a host.
_PROTOCOL_VERSION = "2024-11-05"
# Screenshots can exceed asyncio's default 64 KiB stream limit.
_STREAM_LIMIT = 16 * 1024 * 1024
_CALL_TIMEOUT = 30.0


class StdioMcpClient:
    """One JSON-RPC session over a child process's stdio."""

    def __init__(self, command: list[str], *, env: dict[str, str] | None = None) -> None:
        if not command:
            raise ValueError("MCP sidecar command must be a non-empty argv")
        self._command = command
        # Extra environment merged over the inherited one; None inherits all.
        self._env = {**os.environ, **env} if env else None
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
            env=self._env,
        )
        assert self._proc.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self.request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "tstd", "version": "0.1.0"},
            },
        )
        await self.notify("notifications/initialized")

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        await self.start()
        async with self._lock:
            return await asyncio.wait_for(
                self._exchange(method, params, with_id=True),
                timeout=_CALL_TIMEOUT,
            )

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.start()
        async with self._lock:
            await self._exchange(method, params, with_id=False)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return await self.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
        )

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
            raise DesktopError("cu_error", "computer-use sidecar is not running")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        req_id: int | None = None
        if with_id:
            req_id = self._next_id
            self._next_id += 1
            message["id"] = req_id
        if params is not None:
            message["params"] = params
        payload = json.dumps(message) + "\n"
        proc.stdin.write(payload.encode("utf-8"))
        await proc.stdin.drain()
        if not with_id:
            return None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                raise DesktopError("cu_error", "computer-use sidecar closed stdout")
            try:
                parsed: Any = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict) or parsed.get("id") != req_id:
                continue
            if "error" in parsed:
                err = parsed["error"]
                text = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                raise map_mcp_error(str(text))
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
                log.debug("cu-mcp stderr: %s", line.decode("utf-8", errors="replace").rstrip())
        except asyncio.CancelledError:
            return


def map_mcp_error(message: str) -> DesktopError:
    """Translate a sidecar error string into a typed ``DesktopError``."""
    lower = message.casefold()
    if "expected the foreground window" in lower:
        return DesktopError("focus_mismatch", message)
    if "kill-switch" in lower or "actuation halted" in lower or "actuation is disabled" in lower:
        return DesktopError("cu_killed", message)
    if "e20" in lower or "no computer-use backend" in lower:
        return DesktopError("e20", message)
    if "td-2002" in lower or "xwayland display is not a substitute" in lower:
        return DesktopError(DesktopError.WAYLAND, message)
    if is_uipi_failure(message):
        return DesktopError(DesktopError.UIPI, message)
    if is_secure_desktop_failure(message):
        return DesktopError(DesktopError.SECURE_DESKTOP, message)
    if is_permission_failure(message):
        return DesktopError(DesktopError.PERMISSION_DENIED, message)
    return DesktopError("cu_error", message)
