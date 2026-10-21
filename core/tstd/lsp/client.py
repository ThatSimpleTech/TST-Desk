"""One language server, spoken to over its stdin and stdout (TD-5139).

The server is a child process. TST Desk writes LSP's ``Content-Length``
framed JSON-RPC to its stdin and reads replies and notifications from its
stdout; no socket is opened, so nothing listens on any address. The child
leads its own process group with secrets stripped from its environment,
as the shell tool's commands do, and closing the client kills the group.

Only what the tools need is implemented: ``initialize``, full-text
document sync, ``publishDiagnostics``, and plain requests. A request the
server sends back gets an empty answer (``workspace/configuration``) or
"method not found", so a server never waits on the client.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logging import get_logger
from ..lsp_config import LspServerConfig
from ..proc_lifecycle import kill_left_leader_running, reap_subprocess, retain_subprocess_transport

log = get_logger(__name__)

#: Longest message read from a server; a bigger one is a broken server.
MAX_MESSAGE_BYTES = 32 * 1024 * 1024
_SHUTDOWN_SECS = 2.0
_REAP_SECS = 2.0

_METHOD_NOT_FOUND = -32601


class LspError(Exception):
    """The server could not start, answered with an error, or went away."""


@dataclass
class _Document:
    version: int
    text: str


@dataclass
class _Published:
    """The last diagnostics the server published for one document."""

    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    count: int = 0


class LspClient:
    """A running language server for one workspace root."""

    def __init__(self, server: LspServerConfig, root: Path, env: dict[str, str]) -> None:
        self.server = server
        self.root = root
        self._env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._docs: dict[str, _Document] = {}
        self._published: dict[str, _Published] = {}
        self._changed = asyncio.Condition()
        self._write_lock = asyncio.Lock()
        self.position_encoding = "utf-16"
        self.capabilities: dict[str, Any] = {}
        self.last_used = time.monotonic()
        #: What the model was last told for each document (diagnostics).
        self.reported: dict[str, list[dict[str, Any]]] = {}

    @property
    def alive(self) -> bool:
        """The child is running and its output is still being read."""
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._reader is not None
            and not self._reader.done()
        )

    async def start(self) -> None:
        """Spawn the server and run the ``initialize`` handshake."""
        import shutil

        from ..tools.shell import _CREATE_NEW_PROCESS_GROUP, _CREATE_NO_WINDOW

        argv = self.server.command
        program = shutil.which(argv[0], path=self._env.get("PATH"))
        if program is None:
            raise LspError(f"{argv[0]} is not on PATH")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                program,
                *argv[1:],
                cwd=str(self.root),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._env,
                start_new_session=True,
                creationflags=_CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
                limit=MAX_MESSAGE_BYTES,
            )
        except OSError as e:
            raise LspError(str(e)) from e
        self._reader = asyncio.create_task(self._read_loop(), name=f"lsp-{self.server.label}")
        try:
            result = await self.request(
                "initialize", self._initialize_params(), timeout=self.server.start_timeout_seconds
            )
        except BaseException:
            await self.aclose()
            raise
        caps = result.get("capabilities") if isinstance(result, dict) else None
        self.capabilities = caps if isinstance(caps, dict) else {}
        encoding = self.capabilities.get("positionEncoding")
        if encoding in ("utf-8", "utf-16", "utf-32"):
            self.position_encoding = encoding
        await self.notify("initialized", {})

    def _initialize_params(self) -> dict[str, Any]:
        uri = self.root.as_uri()
        return {
            "processId": None,
            "clientInfo": {"name": "tst-desk"},
            "rootUri": uri,
            "rootPath": str(self.root),
            "workspaceFolders": [{"uri": uri, "name": self.root.name or "workspace"}],
            "initializationOptions": self.server.init_options or None,
            "capabilities": {
                "general": {"positionEncodings": ["utf-16", "utf-8"]},
                "workspace": {"workspaceFolders": True, "configuration": True, "symbol": {}},
                "textDocument": {
                    "synchronization": {"didSave": True, "dynamicRegistration": False},
                    "publishDiagnostics": {"versionSupport": True},
                    "definition": {"linkSupport": True},
                    "references": {},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                },
            },
        }

    # ── wire ─────────────────────────────────────────────────────────

    async def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or not self.alive:
            raise LspError("the language server is not running")
        body = json.dumps({"jsonrpc": "2.0", **message}).encode("utf-8")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        async with self._write_lock:
            try:
                proc.stdin.write(frame)
                await proc.stdin.drain()
            except (ConnectionError, RuntimeError) as e:
                raise LspError(f"the language server went away: {e}") from e

    async def request(self, method: str, params: Any, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        """Send *method* and wait for its result. Raises :class:`LspError`."""
        self.last_used = time.monotonic()
        self._next_id += 1
        rid = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            await self._send({"id": rid, "method": method, "params": params})
            wait = timeout if timeout is not None else self.server.request_timeout_seconds
            try:
                return await asyncio.wait_for(future, timeout=wait)
            except TimeoutError as e:
                with contextlib.suppress(LspError):
                    await self.notify("$/cancelRequest", {"id": rid})
                raise LspError(f"{method} got no answer within {wait:g} s") from e
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: Any) -> None:
        """Send a notification; there is no answer."""
        await self._send({"method": method, "params": params})

    async def _read_message(self, stream: asyncio.StreamReader) -> dict[str, Any] | None:
        length: int | None = None
        while True:
            line = await stream.readline()
            if not line:
                return None
            text = line.decode("ascii", "replace").strip()
            if not text:
                break
            key, _, value = text.partition(":")
            if key.strip().casefold() == "content-length":
                try:
                    length = int(value.strip())
                except ValueError:
                    length = None
        if length is None or length < 0 or length > MAX_MESSAGE_BYTES:
            raise LspError("bad Content-Length from the language server")
        body = await stream.readexactly(length)
        data = json.loads(body.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}

    async def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                message = await self._read_message(proc.stdout)
                if message is None:
                    break
                await self._handle(message)
        except (LspError, ValueError, asyncio.IncompleteReadError) as e:
            log.warning("language server output unreadable: %s", e)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(LspError("the language server exited"))
            async with self._changed:
                self._changed.notify_all()

    async def _handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        rid = message.get("id")
        if method is None:
            future = self._pending.get(rid) if isinstance(rid, int) else None
            if future is None or future.done():
                return
            if "error" in message:
                err = message.get("error") or {}
                text = err.get("message", "error") if isinstance(err, dict) else str(err)
                future.set_exception(LspError(str(text)))
            else:
                future.set_result(message.get("result"))
            return
        if rid is not None:
            await self._answer(rid, str(method), message.get("params"))
            return
        if method == "textDocument/publishDiagnostics":
            await self._on_diagnostics(message.get("params"))

    async def _answer(self, rid: Any, method: str, params: Any) -> None:
        """Answer a request the server sent, so it never waits on us."""
        if method == "workspace/configuration":
            items = params.get("items", []) if isinstance(params, dict) else []
            reply: dict[str, Any] = {"id": rid, "result": [None] * len(items)}
        elif method in (
            "client/registerCapability",
            "client/unregisterCapability",
            "window/workDoneProgress/create",
        ):
            reply = {"id": rid, "result": None}
        elif method == "workspace/workspaceFolders":
            uri = self.root.as_uri()
            reply = {"id": rid, "result": [{"uri": uri, "name": self.root.name or "workspace"}]}
        else:
            reply = {
                "id": rid,
                "error": {"code": _METHOD_NOT_FOUND, "message": f"{method} not supported"},
            }
        with contextlib.suppress(LspError):
            await self._send(reply)

    async def _on_diagnostics(self, params: Any) -> None:
        if not isinstance(params, dict) or not isinstance(params.get("uri"), str):
            return
        raw = params.get("diagnostics")
        diagnostics = [d for d in raw if isinstance(d, dict)] if isinstance(raw, list) else []
        async with self._changed:
            slot = self._published.setdefault(params["uri"], _Published())
            slot.diagnostics = diagnostics
            slot.count += 1
            self._changed.notify_all()

    # ── documents ────────────────────────────────────────────────────

    def publish_count(self, uri: str) -> int:
        """How many times diagnostics were published for *uri* so far."""
        slot = self._published.get(uri)
        return slot.count if slot is not None else 0

    def diagnostics(self, uri: str) -> list[dict[str, Any]] | None:
        """The last published diagnostics for *uri*, or None if none came."""
        slot = self._published.get(uri)
        return list(slot.diagnostics) if slot is not None else None

    async def sync(self, path: Path, text: str, *, saved: bool = False) -> bool:
        """Open *path* or send its whole new text. True when the server was told."""
        uri = path.as_uri()
        doc = self._docs.get(uri)
        if doc is None:
            self._docs[uri] = _Document(version=1, text=text)
            await self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": self.server.language_id,
                        "version": 1,
                        "text": text,
                    }
                },
            )
        elif doc.text != text:
            doc.version += 1
            doc.text = text
            await self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": doc.version},
                    "contentChanges": [{"text": text}],
                },
            )
        elif not saved:
            return False
        if saved:
            await self.notify("textDocument/didSave", {"textDocument": {"uri": uri}})
        return True

    async def wait_for_diagnostics(
        self,
        uri: str,
        after: int,
        timeout: float,  # noqa: ASYNC109
        settle: float = 0.3,
    ) -> list[dict[str, Any]] | None:
        """Diagnostics published for *uri* after publish number *after*.

        Servers often publish twice (syntax first, then types), so once one
        arrives this waits *settle* seconds more for the next. None when
        nothing came within *timeout*.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        seen = after
        settle_until: float | None = None
        async with self._changed:
            while True:
                count = self.publish_count(uri)
                if count > seen:
                    seen = count
                    settle_until = min(deadline, loop.time() + settle)
                stop_at = settle_until if settle_until is not None else deadline
                left = stop_at - loop.time()
                if left <= 0 or not self.alive:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), timeout=left)
        return self.diagnostics(uri) if seen > after else None

    # ── lifetime ─────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """``shutdown`` and ``exit``, then kill the group if it is still up."""
        proc = self._proc
        if proc is None:
            return
        if self.alive:
            with contextlib.suppress(LspError):
                await self.request("shutdown", None, timeout=_SHUTDOWN_SECS)
            with contextlib.suppress(LspError):
                await self.notify("exit", None)
        if proc.returncode is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_SHUTDOWN_SECS)
        if proc.returncode is None:
            from ..tools.shell import _kill_process_group

            note = _kill_process_group(proc)
            if note:
                log.warning("language server kill: %s (pid %s)", note, proc.pid)
            if not kill_left_leader_running(proc, note):
                await reap_subprocess(proc, timeout=_REAP_SECS)
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        retain_subprocess_transport(proc)
