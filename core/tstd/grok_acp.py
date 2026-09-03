"""ACP JSON-RPC client over a stdio subprocess.

Grok Build speaks the Agent Client Protocol on stdin/stdout as NDJSON
(JSON-RPC 2.0, one object per line). TST Desk is the client: it spawns
``grok agent stdio``, answers permission requests, and maps session
updates onto the existing daemon event log.

This module does not talk to any model API. Credentials stay in the
Grok CLI (``~/.grok/auth.json`` / ``XAI_API_KEY``).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .logging import get_logger

log = get_logger("tstd.grok_acp")

PermissionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
UpdateHandler = Callable[[dict[str, Any]], Awaitable[None]]


class GrokEngineError(Exception):
    """The Grok CLI could not be found, started, or spoken to."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def find_grok_binary(configured: str = "") -> Path:
    """Resolve the ``grok`` executable.

    Order: configured path, ``PATH``, then ``~/.grok/bin/grok``.
    """
    candidates: list[Path] = []
    if configured.strip():
        candidates.append(Path(configured.strip()).expanduser())
    which = shutil.which("grok")
    if which:
        candidates.append(Path(which))
    candidates.append(Path.home() / ".grok" / "bin" / "grok")
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    raise GrokEngineError(
        "grok_not_found",
        "Grok CLI not found. Install Grok Build, run `grok login`, "
        "or set engine.binary in config.yaml.",
    )


class AcpClient:
    """One ACP agent process. NDJSON JSON-RPC on stdin/stdout."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 1
        self._permission_handler: PermissionHandler | None = None
        self._update_handler: UpdateHandler | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._closed = False

    def on_permission(self, handler: PermissionHandler) -> None:
        self._permission_handler = handler

    def on_update(self, handler: UpdateHandler) -> None:
        self._update_handler = handler

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and not self._closed

    async def start(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
        self._closed = False
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def initialize(self, client_name: str, client_version: str) -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": client_name, "version": client_version},
            },
        )
        return result if isinstance(result, dict) else {}

    async def session_new(
        self,
        cwd: str,
        *,
        yolo: bool = False,
        resume_id: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> str:
        servers = mcp_servers if mcp_servers is not None else []
        if resume_id:
            result = await self.request(
                "session/load",
                {"sessionId": resume_id, "cwd": cwd, "mcpServers": servers},
            )
            if isinstance(result, dict) and result.get("sessionId"):
                return str(result["sessionId"])
            return resume_id
        params: dict[str, Any] = {"cwd": cwd, "mcpServers": servers}
        if yolo:
            params["_meta"] = {"yoloMode": True}
        result = await self.request("session/new", params)
        if not isinstance(result, dict) or not result.get("sessionId"):
            raise GrokEngineError("session_failed", "Grok agent did not return a sessionId")
        return str(result["sessionId"])

    async def prompt(
        self,
        session_id: str,
        text: str,
        images: list[tuple[str, bytes, str]] | None = None,
    ) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for _name, raw, media in images or []:
            blocks.append(
                {
                    "type": "image",
                    "mimeType": media,
                    "data": base64.b64encode(raw).decode("ascii"),
                }
            )
        if not blocks:
            blocks.append({"type": "text", "text": ""})
        result = await self.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": blocks},
        )
        return result if isinstance(result, dict) else {}

    async def set_mode(self, session_id: str, mode_id: str) -> Any:
        return await self.request(
            "session/set_mode",
            {"sessionId": session_id, "modeId": mode_id},
        )

    async def cancel(self, session_id: str) -> None:
        await self.notify("session/cancel", {"sessionId": session_id})

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._closed or self._proc is None or self._proc.stdin is None:
            raise GrokEngineError("closed", "ACP client is closed")
        req_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._write(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        )
        try:
            return await fut
        except asyncio.CancelledError:
            self._pending.pop(req_id, None)
            raise

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        proc = self._proc
        if proc is not None and proc.stdin is not None:
            proc.stdin.close()
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()

    async def _write(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise GrokEngineError("closed", "ACP client is closed")
        payload = json.dumps(message, separators=(",", ":")) + "\n"
        proc.stdin.write(payload.encode())
        await proc.stdin.drain()

    async def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    log.warning(
                        "ACP stdout was not JSON",
                        extra={"extra_fields": {"line": text[:200]}},
                    )
                    continue
                if not isinstance(message, dict):
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            return
        finally:
            self._fail_pending("agent_exited", "Grok agent process closed stdout")

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    log.info("grok stderr", extra={"extra_fields": {"line": text[:500]}})
        except asyncio.CancelledError:
            return

    async def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        msg_id = message.get("id")
        if method == "session/update":
            params = message.get("params")
            if isinstance(params, dict) and self._update_handler is not None:
                await self._update_handler(params)
            return
        if method == "session/request_permission" and msg_id is not None:
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            assert isinstance(params, dict)
            try:
                result = (
                    await self._permission_handler(params)
                    if self._permission_handler is not None
                    else _reject_permission()
                )
                await self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})
            except Exception as exc:
                log.exception("permission handler failed")
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32000, "message": str(exc)},
                    }
                )
            return
        if method and msg_id is not None:
            # Unknown agent→client request: refuse rather than hang.
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )
            return
        if msg_id is not None:
            fut = self._pending.pop(int(msg_id), None)
            if fut is None or fut.done():
                return
            if "error" in message:
                err = message["error"]
                text = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                fut.set_exception(GrokEngineError("rpc_error", text))
            else:
                fut.set_result(message.get("result"))

    def _fail_pending(self, code: str, message: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(GrokEngineError(code, message))
        self._pending.clear()


def _reject_permission() -> dict[str, Any]:
    return {"outcome": {"outcome": "cancelled"}}


def pick_permission_option(
    options: list[dict[str, Any]],
    *,
    approved: bool,
) -> dict[str, Any]:
    """Choose an ACP permission option matching an approve/deny."""

    def _kind(option: dict[str, Any]) -> str:
        return str(option.get("kind") or option.get("optionId") or "").lower()

    if approved:
        for option in options:
            kind = _kind(option)
            if "allow_once" in kind or kind.endswith("allow-once") or "allow-once" in kind:
                return option
        for option in options:
            if "allow" in _kind(option) and "always" not in _kind(option):
                return option
        for option in options:
            if "allow" in _kind(option):
                return option
    else:
        for option in options:
            kind = _kind(option)
            if "reject" in kind or "deny" in kind:
                return option
    if options:
        return options[0]
    return {"optionId": "allow-once" if approved else "reject-once"}
