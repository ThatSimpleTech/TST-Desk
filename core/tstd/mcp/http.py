"""HTTP JSON-RPC client for a user-listed loopback MCP server."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import is_loopback_url
from .errors import McpError
from .jsonrpc import (
    CALL_TIMEOUT,
    McpRemoteTool,
    ProgressCallback,
    cancelled_notification,
    initialize_params,
    jsonrpc_error_text,
    parse_tools_list,
)

_HTTP_FIX = "Set mcp.servers.<id>.url to a loopback http(s) URL (127.0.0.1, ::1, or localhost)."


def assert_loopback_http_url(url: str) -> None:
    """Refuse a URL we must never dial.

    Destination is always ``server.url`` from config. Off-box is a
    load-time refusal, not a request.
    """
    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise McpError(f"MCP url is not a URL: {e}", fix=_HTTP_FIX) from e
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise McpError("MCP url must be an http(s) endpoint", fix=_HTTP_FIX)
    if not is_loopback_url(url):
        raise McpError(
            "MCP url must be loopback; off-box destinations are refused",
            fix=_HTTP_FIX,
        )


class HttpMcpClient:
    """One JSON-RPC session as POST-to-configured-URL.

    Not SSE and not Streamable-HTTP session ids — one JSON object out,
    one JSON object back. The URL is the only host this module dials.
    """

    def __init__(self, url: str, *, timeout: float = CALL_TIMEOUT) -> None:
        assert_loopback_http_url(url)
        self._url = url
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None
        self._next_id = 1
        self._pending: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout + 5.0)
        await self.request("initialize", initialize_params())
        await self.notify("notifications/initialized")

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout + 5.0)
        req_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        data = await self._post(payload)
        if not isinstance(data, dict):
            raise McpError("MCP HTTP response was not a JSON object")
        if data.get("id") not in (req_id, None):
            raise McpError("MCP HTTP response id did not match the request")
        if "error" in data:
            raise McpError(jsonrpc_error_text(data["error"]))
        return data.get("result")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout + 5.0)
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._post(payload, require_json=False)

    async def list_tools(self) -> list[McpRemoteTool]:
        return parse_tools_list(await self.request("tools/list"))

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> Any:
        """``tools/call`` bounded by the call timeout (TD-5105).

        One JSON object back carries no progress, so the timeout bounds the
        whole call here; progress needs the SSE transport (TD-5136).  A
        timeout or a stopped turn sends ``notifications/cancelled``.
        """
        del on_progress
        req_id = self._next_id  # request() takes this id before it awaits
        try:
            return await asyncio.wait_for(
                self.request("tools/call", {"name": name, "arguments": arguments or {}}),
                timeout=self._timeout,
            )
        except TimeoutError:
            reason = f"no result for {self._timeout:g} s (timeout_seconds)"
            self._cancel_later(req_id, reason)
            raise McpError(f"MCP tool {name!r} cancelled: {reason}") from None
        except asyncio.CancelledError:
            self._cancel_later(req_id, "the user stopped the turn")
            raise

    def _cancel_later(self, req_id: int, reason: str) -> None:
        """Post ``notifications/cancelled`` without holding up the caller."""

        async def post() -> None:
            with contextlib.suppress(McpError, TimeoutError):
                await asyncio.wait_for(
                    self._post(cancelled_notification(req_id, reason), require_json=False),
                    timeout=5.0,
                )

        task = asyncio.get_running_loop().create_task(post())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def aclose(self) -> None:
        client = self._http
        self._http = None
        if client is not None:
            await client.aclose()

    async def _post(self, payload: dict[str, Any], *, require_json: bool = True) -> Any:
        if self._http is None:
            raise McpError("HTTP MCP client is not running")
        try:
            response = await self._http.post(
                self._url,
                json=payload,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise McpError(f"MCP HTTP request failed: {e}") from e
        if not response.content:
            if require_json:
                raise McpError("MCP HTTP response was empty")
            return None
        try:
            return response.json()
        except json.JSONDecodeError as e:
            if require_json:
                raise McpError("MCP HTTP response was not JSON") from e
            return None
