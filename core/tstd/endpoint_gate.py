"""How many model calls one server answers at a time (TD-5269).

Nothing bounded model calls across the daemon. A ``workflow`` call limits
its own children, and that was all: two chats, a scheduled run and a
workflow could all ask one local server at once, and a single GPU then
serves all of them slowly or fails some. Here each server gets one count,
shared by every caller in the daemon.

A gate is keyed by the server, not the model or the key: scheme, host and
port, with every spelling of this computer folded to one host. Two paths
on one port are one server, and a GPU does not care which key asked.

No limit is set unless config sets one, so nothing changes on upgrade. A
slot is held for one HTTP exchange only (see ``ProviderClient``): never
across a retry's wait, and never while a tool runs, so a call that holds a
slot is never waiting on another call for the same server.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from .net import is_loopback_url

_LOOPBACK = "this-computer"
_DEFAULT_PORTS = {"http": 80, "https": 443}


class EndpointLimit(BaseModel):
    """One server's own limit, in place of the default for its kind."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    max_concurrent: int = Field(ge=1, le=64)


class ModelConcurrencyConfig(BaseModel):
    """The ``model_concurrency`` section of ``config.yaml`` (TD-5269).

    ``loopback_default`` is how many calls a server on this computer
    answers at once; ``remote_default`` the same for any other. ``null``
    is no limit, which is how it was before the key existed. ``endpoints``
    names a server by URL and gives it a number of its own.
    """

    model_config = ConfigDict(extra="forbid")

    loopback_default: int | None = Field(default=None, ge=1, le=64)
    remote_default: int | None = Field(default=None, ge=1, le=64)
    endpoints: list[EndpointLimit] = Field(default_factory=list)


def endpoint_key(url: str) -> str | None:
    """The server *url* names: its scheme, host and port. None when it names none.

    Every loopback spelling (``localhost``, ``127.0.0.1``, ``::1``) is one
    host. The path and any credential in the URL are left out.
    """
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or not host:
        return None
    name = _LOOPBACK if is_loopback_url(url) else host.lower()
    return f"{parts.scheme}://{name}:{port or _DEFAULT_PORTS[parts.scheme]}"


class EndpointGates:
    """One count per server, shared by every client in the daemon."""

    def __init__(self, config: ModelConcurrencyConfig | None = None) -> None:
        self._config = config or ModelConcurrencyConfig()
        # key -> (limit, the semaphore every client for that server shares)
        self._gates: dict[str, tuple[int, asyncio.Semaphore]] = {}

    def configure(self, config: ModelConcurrencyConfig) -> None:
        """Take a reloaded config. A server whose number did not change keeps
        its gate, calls in flight included; one whose number changed gets a
        new gate the next time a client is built for it."""
        self._config = config

    def limit_for(self, base_url: str) -> int | None:
        """The limit config gives the server at *base_url*, or None for none."""
        key = endpoint_key(base_url)
        if key is None:
            return None
        for entry in self._config.endpoints:
            if endpoint_key(entry.url) == key:
                return entry.max_concurrent
        if is_loopback_url(base_url):
            return self._config.loopback_default
        return self._config.remote_default

    def for_url(self, base_url: str) -> asyncio.Semaphore | None:
        """The gate for the server at *base_url*, or None when it has no limit."""
        limit = self.limit_for(base_url)
        key = endpoint_key(base_url)
        if limit is None or key is None:
            return None
        held = self._gates.get(key)
        if held is None or held[0] != limit:
            held = (limit, asyncio.Semaphore(limit))
            self._gates[key] = held
        return held[1]
