"""The daemon's running language servers, one per workspace and server (TD-5139).

A server starts the first time a session needs it for a file its
patterns match, and is shared by every session in the same workspace
root. One that sat unused for ``lsp.idle_seconds`` is shut down the next
time any server is asked for, and the daemon closes them all when it
stops. A server that failed to start is not retried until its
configuration changes or the daemon restarts, so a missing program is
reported once instead of on every write.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from ..logging import get_logger
from ..lsp_config import LspConfig, LspServerConfig
from .client import LspClient, LspError

log = get_logger(__name__)

_Key = tuple[str, str]


class LspStartFailed(LspError):
    """The server did not start. ``first`` is True only the first time."""

    def __init__(self, message: str, *, first: bool) -> None:
        super().__init__(message)
        self.first = first


class LspPool:
    """Running :class:`LspClient` objects keyed by root and server config."""

    def __init__(self) -> None:
        self._clients: dict[_Key, LspClient] = {}
        self._failed: dict[_Key, str] = {}
        self._locks: dict[_Key, asyncio.Lock] = {}

    @staticmethod
    def _key(root: Path, server: LspServerConfig) -> _Key:
        return (str(root), server.model_dump_json())

    async def client(self, config: LspConfig, server: LspServerConfig, root: Path) -> LspClient:
        """The running server for *root*, started if needed.

        Raises:
            LspStartFailed: it could not start (now or on an earlier try).
        """
        await self._reap_idle(config.idle_seconds)
        key = self._key(root, server)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            running = self._clients.get(key)
            if running is not None and running.alive:
                running.last_used = time.monotonic()
                return running
            if running is not None:
                self._clients.pop(key, None)
                await running.aclose()
            if key in self._failed:
                raise LspStartFailed(self._failed[key], first=False)
            from ..tools.shell import sanitized_env

            client = LspClient(server, root, sanitized_env())
            try:
                await client.start()
            except LspError as e:
                self._failed[key] = str(e)
                log.warning(
                    "language server did not start",
                    extra={"extra_fields": {"server": server.label, "error": str(e)}},
                )
                raise LspStartFailed(str(e), first=True) from e
            log.info(
                "language server started",
                extra={"extra_fields": {"server": server.label, "root": str(root)}},
            )
            self._clients[key] = client
            return client

    async def _reap_idle(self, idle_seconds: float) -> None:
        now = time.monotonic()
        stale = [k for k, c in self._clients.items() if now - c.last_used > idle_seconds]
        for key in stale:
            client = self._clients.pop(key)
            log.info("language server idle; stopping", extra={"extra_fields": {"key": key[0]}})
            await client.aclose()

    @property
    def running(self) -> int:
        """How many servers are up."""
        return sum(1 for c in self._clients.values() if c.alive)

    async def aclose(self) -> None:
        """Stop every server. Failed starts are forgotten too."""
        clients = list(self._clients.values())
        self._clients.clear()
        self._failed.clear()
        await asyncio.gather(*(c.aclose() for c in clients), return_exceptions=True)


_POOL = LspPool()


def default_pool() -> LspPool:
    """The daemon's one pool."""
    return _POOL
