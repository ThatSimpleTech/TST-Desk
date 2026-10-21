"""Streamable HTTP client for a user-listed MCP server (TD-4401, TD-5136)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from typing import Any

import httpx
from httpx_sse import EventSource

from ..logging import get_logger
from .answers import (
    BadAnswer,
    OnMessage,
    json_answer,
    parse_message,
    refusal,
    sse_answer,
)
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
from .era import Negotiated, NotModernError, negotiate
from .errors import McpError
from .headers import mirrored_params, modern_headers
from .jsonrpc import (
    CALL_TIMEOUT,
    MAX_CALL_SECONDS,
    ExcludedTool,
    McpRemoteTool,
    ProgressCallback,
    cancelled_notification,
    progress_update,
)
from .urls import assert_http_url

log = get_logger("tstd.mcp.http")

#: Seconds between attempts to reopen a notification stream that dropped.
LISTEN_RETRY_SECONDS = 2.0


class HttpMcpClient:
    """One MCP server over Streamable HTTP, in either protocol era.

    Every message is a POST with ``Accept: application/json,
    text/event-stream``; a response may be one JSON object or an SSE
    stream of notifications ending in the answer. Legacy servers get the
    ``Mcp-Session-Id`` they assign and ``MCP-Protocol-Version`` from
    2025-06-18; modern ones get the per-request metadata headers. The URL
    is the only host this module dials, so ``headers`` (this server's own
    values, TD-5110) go nowhere else.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = CALL_TIMEOUT,
        max_call_seconds: float = MAX_CALL_SECONDS,
        startup_timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
        allow_remote: bool = False,
    ) -> None:
        assert_http_url(url, allow_remote=allow_remote)
        self._url = url
        self._user_headers = dict(headers or {})
        self._timeout = timeout
        self._max_call = max(max_call_seconds, timeout)
        # The handshake (probe, initialize, tools/list) answers in its own
        # time, which may be longer than a call's (TD-5105).
        self._startup_timeout = startup_timeout or timeout
        self._http: httpx.AsyncClient | None = None
        self._next_id = 1
        self._pending: set[asyncio.Task[None]] = set()
        self._start_lock = asyncio.Lock()
        self._session_id: str | None = None
        self._mirrored: dict[str, dict[tuple[str, ...], str]] = {}
        self._listener: asyncio.Task[None] | None = None
        self.negotiated: Negotiated | None = None
        #: Tools ``tools/list`` offered but this transport must refuse.
        self.excluded: list[ExcludedTool] = []
        #: Set by the supervisor; called when the server's tools change.
        self.on_tools_changed: ToolsChanged | None = None

    def _open(self) -> None:
        """Make the client once.

        The explicit transport is what keeps ``headers`` on the configured
        URL: given one, httpx skips its proxy lookup (``HTTP_PROXY``,
        ``ALL_PROXY``, the macOS or Windows system proxy), which covers
        loopback too unless ``NO_PROXY`` lists it, and the proxy may be off
        the box (TD-5110). ``trust_env=False`` would do that as well, but it
        also drops ``SSL_CERT_FILE``, which an https server's own CA may
        rely on.
        """
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self._timeout + 5.0, transport=httpx.AsyncHTTPTransport()
            )

    async def start(self) -> None:
        async with self._start_lock:
            if self.negotiated is not None:
                return
            self._open()
            startup = self._startup_timeout
            negotiated = await negotiate(
                self._raw_request,
                probe_timeout=startup,
                startup_timeout=startup,
                timeout_means_legacy=False,
            )
            self.negotiated = negotiated
            if negotiated.era == "legacy":
                await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, startup)
                self._listener = asyncio.create_task(self._listen_legacy())
            elif negotiated.tools_list_changed:
                self._listener = asyncio.create_task(self._listen_modern())

    @property
    def startup_seconds(self) -> float:
        """Longest :meth:`start` may take: the probe, then the handshake."""
        return 2 * self._startup_timeout

    def _take_id(self) -> int:
        req_id = self._next_id
        self._next_id += 1
        return req_id

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        wait_seconds: float | None = None,
    ) -> Any:
        await self.start()
        assert self.negotiated is not None
        wait = wait_seconds or self._timeout
        payload = self._payload(self._take_id(), method, self.negotiated.stamp(params))
        return checked_result(await asyncio.wait_for(self._post(payload, wait), timeout=wait))

    async def list_tools(self) -> list[McpRemoteTool]:
        """``tools/list``, every page, with the startup timeout.

        A modern server's tool whose ``x-mcp-header`` marks break the
        spec's rules is left out and recorded in :attr:`excluded`.
        """
        startup = self._startup_timeout
        listed = await list_all_tools(
            lambda params: self.request("tools/list", params, wait_seconds=startup)
        )
        if self.negotiated is None or self.negotiated.era != "modern":
            return listed
        kept: list[McpRemoteTool] = []
        self.excluded = []
        self._mirrored = {}
        for tool in listed:
            try:
                self._mirrored[tool.name] = mirrored_params(tool.parameters)
            except ValueError as e:
                self.excluded.append(ExcludedTool(tool.name, str(e)))
                continue
            kept.append(tool)
        return kept

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> Any:
        """``tools/call`` with a progress token (TD-5105, TD-5136).

        A server that answers with an SSE stream can report progress; each
        notification for this call's token resets the idle timeout, up to
        the hard cap, as on stdio. A timeout or a stopped turn closes the
        stream (the modern cancel) and, for a legacy server, also posts
        ``notifications/cancelled``.
        """
        await self.start()
        assert self.negotiated is not None
        # Taken here, so the cancel notice names this call's request
        # (TD-5154).
        req_id = self._take_id()
        token = f"tstd-{req_id}"
        params = self.negotiated.stamp(tool_call_params(name, arguments or {}, token))
        payload = self._payload(req_id, "tools/call", params)
        progressed = asyncio.Event()

        async def on_message(message: dict[str, Any]) -> None:
            update = progress_update(message, token)
            if update is None:
                await self._on_server_message(message)
                return
            progressed.set()
            if on_progress is not None:
                await on_progress(*update)

        work = asyncio.ensure_future(self._post(payload, self._timeout, on_message))
        try:
            result = await watch_call(work, progressed, idle=self._timeout, hard=self._max_call)
        except CallTimedOut as e:
            self._cancel_later(req_id, str(e))
            raise McpError(f"MCP tool {name!r} cancelled: {e}") from None
        except asyncio.CancelledError:
            self._cancel_later(req_id, "the user stopped the turn")
            raise
        return checked_result(result)

    @staticmethod
    def _payload(req_id: int, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        return payload

    def _cancel_later(self, req_id: int, reason: str) -> None:
        """Post ``notifications/cancelled`` to a legacy server, without waiting.

        A modern server takes the closed response stream as the cancel.
        """
        if self.negotiated is not None and self.negotiated.era == "modern":
            return

        async def post() -> None:
            with contextlib.suppress(McpError, TimeoutError):
                await asyncio.wait_for(
                    self._post(cancelled_notification(req_id, reason), 5.0), timeout=5.0
                )

        task = asyncio.get_running_loop().create_task(post())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def aclose(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener
        client = self._http
        if client is not None and self._session_id is not None:
            # Ending a legacy session is a courtesy; a 405 or an outage is fine.
            with contextlib.suppress(httpx.HTTPError):
                await client.delete(self._url, headers=self._headers({}), timeout=5.0)
        self._session_id = None
        self._http = None
        if client is not None:
            await client.aclose()

    # ── wire ────────────────────────────────────────────────────────────

    def _headers(self, payload: dict[str, Any]) -> httpx.Headers:
        """This server's own headers, then the protocol's, which win."""
        headers = httpx.Headers(self._user_headers)
        headers["Accept"] = "application/json, text/event-stream"
        headers["Content-Type"] = "application/json"
        params = payload.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        mirrored = self._mirrored.get(name, {}) if isinstance(name, str) else {}
        modern = modern_headers(payload, mirrored)
        if modern:
            headers.update(modern)
            return headers
        negotiated = self.negotiated
        if negotiated is not None and negotiated.era == "legacy":
            if negotiated.sends_version_header:
                headers["MCP-Protocol-Version"] = negotiated.version
            if self._session_id is not None:
                headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _raw_request(self, method: str, params: dict[str, Any] | None, wait: float) -> Any:
        """One request for the era probe; a non-JSON-RPC reply means legacy."""
        payload = self._payload(self._take_id(), method, params)
        try:
            return await asyncio.wait_for(self._post(payload, wait), timeout=wait)
        except BadAnswer as e:
            raise NotModernError(str(e)) from e

    async def _post(
        self, payload: dict[str, Any], wait: float, on_message: OnMessage | None = None
    ) -> Any:
        """POST *payload*; the answer's result, or ``None`` for a notification.

        *wait* bounds each read, plus a grace so an outer timeout fires
        first. Messages on an SSE response other than the answer go to
        *on_message* (default: :meth:`_on_server_message`).
        """
        if self._http is None:
            raise McpError("HTTP MCP client is not running")
        handler = on_message or self._on_server_message
        try:
            async with self._http.stream(
                "POST",
                self._url,
                json=payload,
                headers=self._headers(payload),
                timeout=wait + 5.0,
            ) as response:
                if payload.get("method") == "initialize":
                    self._session_id = response.headers.get("mcp-session-id") or None
                return await self._answer(response, payload, handler)
        except httpx.HTTPError as e:
            raise McpError(f"MCP HTTP request failed: {e}") from e

    async def _answer(
        self, response: httpx.Response, payload: dict[str, Any], on_message: OnMessage
    ) -> Any:
        req_id = payload.get("id")
        if response.status_code >= 400:
            await self._refusal(response)
        if req_id is None or response.status_code == 202 or "method" not in payload:
            return None  # a notification, or our reply to the server
        if "text/event-stream" in response.headers.get("content-type", ""):
            events = (event.data async for event in EventSource(response).aiter_sse())
            return await sse_answer(events, req_id, on_message)
        return json_answer(await response.aread(), req_id)

    @staticmethod
    async def _refusal(response: httpx.Response) -> None:
        raise refusal(response.status_code, await response.aread())

    async def _on_server_message(self, message: dict[str, Any]) -> None:
        """A message that is not an answer: a legacy server's request, or a change."""
        if "method" not in message:
            return
        if "id" in message:
            with contextlib.suppress(McpError):
                await self._post(reply_to_server_request(message), 5.0)
            return
        if is_tools_changed(message) and self.on_tools_changed is not None:
            self.on_tools_changed()

    # ── change notifications ────────────────────────────────────────────

    async def _listen_legacy(self) -> None:
        """The legacy GET stream, where a server sends what no request asked for."""
        headers = self._headers({})
        headers["Accept"] = "text/event-stream"
        del headers["Content-Type"]
        while self._http is not None:
            try:
                async with self._http.stream(
                    "GET",
                    self._url,
                    headers=headers,
                    timeout=httpx.Timeout(self._timeout + 5.0, read=None),
                ) as response:
                    kind = response.headers.get("content-type", "")
                    if response.status_code >= 400 or not kind.startswith("text/event-stream"):
                        return  # 405 or not a stream: this server offers none
                    async for event in EventSource(response).aiter_sse():
                        message = parse_message(event.data)
                        if message is not None:
                            await self._on_server_message(message)
            except httpx.HTTPError:
                log.debug("mcp http notification stream dropped", exc_info=True)
            await asyncio.sleep(LISTEN_RETRY_SECONDS)

    async def _listen_modern(self) -> None:
        """``subscriptions/listen`` for ``tools/list_changed``, reopened if it drops."""
        while self._http is not None and self.negotiated is not None:
            params = self.negotiated.stamp({"notifications": {"toolsListChanged": True}})
            payload = self._payload(self._take_id(), "subscriptions/listen", params)
            try:
                async with self._http.stream(
                    "POST",
                    self._url,
                    json=payload,
                    headers=self._headers(payload),
                    timeout=httpx.Timeout(self._timeout + 5.0, read=None),
                ) as response:
                    await self._answer(response, payload, self._on_server_message)
            except McpError as e:
                log.debug("mcp subscriptions/listen ended: %s", e)
                if isinstance(e, NotModernError) or getattr(e, "code", None) is not None:
                    return  # the server refuses it; asking again will not help
            except httpx.HTTPError:
                log.debug("mcp subscriptions/listen dropped", exc_info=True)
            await asyncio.sleep(LISTEN_RETRY_SECONDS)
