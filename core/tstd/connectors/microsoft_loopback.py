"""One-shot loopback redirect for the authorization code (TD-4853).

The listener binds 127.0.0.1 and no other address (prime directive 1).
It closes after the redirect, or after a timeout, so the port is not a
server. A request that is not the redirect — a browser asking for a
favicon — does not consume that one response.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

from .microsoft_models import TIMEOUT_MESSAGE, Callback, MicrosoftError

_PAGE = (
    b'<!DOCTYPE html><html><head><meta charset="utf-8"><title>TST Desk</title></head>'
    b"<body><p>TST Desk is signed in. You can close this window.</p></body></html>"
)
_MAX_STRANGERS = 4
_READ_LIMIT = 8192
_FIELD_LIMIT = 2048


def _response(status: bytes, body: bytes, content_type: bytes | None) -> bytes:
    head = b"HTTP/1.1 " + status + b"\r\nConnection: close\r\n"
    if content_type is None:
        return head + b"Content-Length: 0\r\n\r\n"
    return (
        head
        + b"Content-Type: "
        + content_type
        + b"\r\nContent-Length: "
        + str(len(body)).encode("ascii")
        + b"\r\n\r\n"
        + body
    )


_OK = _response(b"200 OK", _PAGE, b"text/html; charset=utf-8")
_MISSED = _response(b"404 Not Found", b"", None)


def _clip(value: str) -> str:
    """An oversized field is dropped. It is not logged and not exchanged."""
    if len(value) > _FIELD_LIMIT:
        return ""
    return value


def _first(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name) or []
    return values[0] if values else ""


def callback_from_request(raw: bytes) -> Callback | None:
    """The redirect, or None when this request is not the authorization response.

    ``error_description`` is ignored. Providers put the code in it.
    """
    line = raw.split(b"\r\n", 1)[0]
    parts = line.decode("latin-1", errors="replace").split(" ")
    target = parts[1] if len(parts) >= 2 else ""
    parsed = urlsplit(target)
    if parsed.path not in {"", "/"}:
        return None
    query = parse_qs(parsed.query, max_num_fields=8)
    if not any(key in query for key in ("code", "state", "error")):
        return None
    return Callback(
        code=_clip(_first(query, "code")),
        state=_clip(_first(query, "state")),
        error=_clip(_first(query, "error")),
    )


class LoopbackAuth:
    """Bind an ephemeral port on 127.0.0.1 and take a single redirect."""

    def __init__(self) -> None:
        self.bound_host = ""
        self.port = 0
        self._server: asyncio.Server | None = None
        self._future: asyncio.Future[Callback] | None = None
        self._used = False
        self._strangers = 0

    async def start(self) -> str:
        """Start listening. The redirect URI is the bound loopback port."""
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()
        # Host is the literal loopback address. Port 0 asks the OS for a free one.
        self._server = await asyncio.start_server(self._on_connection, "127.0.0.1", 0)
        socks = tuple(self._server.sockets or ())
        if not socks or any(sock.getsockname()[0] != "127.0.0.1" for sock in socks):
            await self.aclose()
            raise MicrosoftError("Sign-in could not listen on this machine.")
        host, port = socks[0].getsockname()[:2]
        self.bound_host = str(host)
        self.port = int(port)
        return f"http://127.0.0.1:{self.port}"

    async def wait(self, timeout_seconds: float) -> Callback:
        """Block until the redirect arrives or *timeout_seconds* pass, then close."""
        future = self._future
        if future is None:
            raise MicrosoftError("Sign-in could not listen on this machine.")
        try:
            return await asyncio.wait_for(future, timeout_seconds)
        except TimeoutError:
            raise MicrosoftError(TIMEOUT_MESSAGE, phase="expired") from None
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Stop accepting. Safe to call more than once."""
        server = self._server
        self._server = None
        if server is None:
            return
        server.close()
        await server.wait_closed()

    def _finish(self, callback: Callback) -> None:
        future = self._future
        if future is not None and not future.done():
            future.set_result(callback)

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            if self._used:
                await _write(writer, _MISSED)
                return
            try:
                raw = await asyncio.wait_for(reader.read(_READ_LIMIT), timeout=5)
            except TimeoutError:
                raw = b""
            callback = callback_from_request(raw)
            if callback is None:
                self._strangers += 1
                await _write(writer, _MISSED)
                # A scanner must not keep the port open until the sign-in timeout.
                if self._strangers >= _MAX_STRANGERS:
                    self._used = True
                    self._finish(Callback(code="", state="", error=""))
                return
            self._used = True
            await _write(writer, _OK)
            self._finish(callback)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError, RuntimeError):
                return


async def _write(writer: asyncio.StreamWriter, payload: bytes) -> None:
    writer.write(payload)
    try:
        await writer.drain()
    except (ConnectionError, OSError, RuntimeError):
        return
