"""MCP protocol version and era negotiation (TD-5136).

MCP has two eras. Legacy revisions (2024-11-05 to 2025-11-25) open with an
``initialize`` handshake and keep the negotiated version for the session.
Modern revisions (2026-07-28 on) have no handshake: every request carries
the version, the client's identity and its capabilities in ``_meta``.

tstd speaks both. It probes with ``server/discover`` at its newest modern
version, and anything that is not positive evidence of a modern server
falls back to ``initialize`` offering the newest legacy version, so a
server that only speaks 2024-11-05 keeps working.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import McpError

#: Revisions reached through ``initialize``, oldest first.
LEGACY_VERSIONS: tuple[str, ...] = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
#: Revisions that carry the version in every request's ``_meta``.
MODERN_VERSIONS: tuple[str, ...] = ("2026-07-28",)
LATEST_LEGACY = LEGACY_VERSIONS[-1]
LATEST_MODERN = MODERN_VERSIONS[-1]
#: First legacy revision whose HTTP requests carry ``MCP-Protocol-Version``.
VERSION_HEADER_SINCE = "2025-06-18"

CLIENT_NAME = "tstd"
CLIENT_VERSION = "0.3.0"
#: What tstd does for a server. It answers ``ping`` and nothing else a
#: server may ask of a client: no roots, sampling or elicitation. An honest
#: empty set means a server never sends one of those and waits on it.
CLIENT_CAPABILITIES: dict[str, Any] = {}

PROTOCOL_VERSION_META = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"
SUBSCRIPTION_ID_META = "io.modelcontextprotocol/subscriptionId"

HEADER_MISMATCH = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022
METHOD_NOT_FOUND = -32601
#: Errors only a modern server sends: they settle the era, never fall back.
MODERN_ERRORS = frozenset(
    {HEADER_MISMATCH, MISSING_REQUIRED_CLIENT_CAPABILITY, UNSUPPORTED_PROTOCOL_VERSION}
)

#: Longest the stdio probe waits: a legacy server may never answer a
#: request sent before ``initialize``.
PROBE_TIMEOUT = 2.0

Era = Literal["legacy", "modern"]


class McpRpcError(McpError):
    """A JSON-RPC error object from the server."""

    def __init__(self, code: int | None, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class NotModernError(McpError):
    """An HTTP answer to the probe that names no modern error (legacy server)."""


def rpc_error(error: Any) -> McpRpcError:
    """The typed error for a JSON-RPC ``error`` member."""
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        return McpRpcError(
            code if isinstance(code, int) else None,
            message if isinstance(message, str) and message else str(error),
            error.get("data"),
        )
    return McpRpcError(None, str(error))


def client_info() -> dict[str, str]:
    return {"name": CLIENT_NAME, "version": CLIENT_VERSION}


def modern_meta(version: str) -> dict[str, Any]:
    """The per-request ``_meta`` a modern request carries."""
    return {
        PROTOCOL_VERSION_META: version,
        CLIENT_INFO_META: client_info(),
        CLIENT_CAPABILITIES_META: dict(CLIENT_CAPABILITIES),
    }


def initialize_params(version: str = LATEST_LEGACY) -> dict[str, Any]:
    """Body for the legacy ``initialize`` request."""
    return {
        "protocolVersion": version,
        "capabilities": dict(CLIENT_CAPABILITIES),
        "clientInfo": client_info(),
    }


@dataclass(frozen=True)
class Negotiated:
    """The era and version a server agreed to, and what it can do."""

    era: Era
    version: str
    capabilities: dict[str, Any] = field(default_factory=dict)

    def stamp(self, params: dict[str, Any] | None) -> dict[str, Any] | None:
        """*params* as sent: a modern request gets the ``_meta`` envelope."""
        if self.era != "modern":
            return params
        out = dict(params or {})
        meta = out.get("_meta")
        out["_meta"] = {**(meta if isinstance(meta, dict) else {}), **modern_meta(self.version)}
        return out

    @property
    def tools_list_changed(self) -> bool:
        tools = self.capabilities.get("tools")
        return isinstance(tools, dict) and tools.get("listChanged") is True

    @property
    def sends_version_header(self) -> bool:
        if self.era == "modern":
            return True
        return LEGACY_VERSIONS.index(self.version) >= LEGACY_VERSIONS.index(VERSION_HEADER_SINCE)


#: Send one request and return its result. Raises :class:`McpRpcError` for
#: a JSON-RPC error, :class:`NotModernError` for an HTTP refusal without
#: one, and ``TimeoutError`` past *timeout* seconds.
RawRequest = Callable[[str, dict[str, Any] | None, float], Awaitable[Any]]


def _supported(error: McpRpcError) -> list[str]:
    data = error.data
    raw = data.get("supported") if isinstance(data, dict) else None
    return [v for v in raw if isinstance(v, str)] if isinstance(raw, list) else []


def _mutual_modern(versions: list[str]) -> list[str]:
    return [v for v in MODERN_VERSIONS if v in versions]


def _speaks_nothing_we_do(versions: list[str]) -> McpError:
    return McpError(
        f"the server speaks protocol versions {', '.join(versions) or 'none named'}; "
        f"tstd speaks {', '.join((*LEGACY_VERSIONS, *MODERN_VERSIONS))}",
        fix="Update the MCP server, or tstd, so they share a protocol version.",
    )


async def negotiate(
    request: RawRequest,
    *,
    probe_timeout: float,
    startup_timeout: float,
    timeout_means_legacy: bool,
) -> Negotiated:
    """Probe for a modern server, else run the legacy handshake.

    The caller sends ``notifications/initialized`` after a legacy result.
    A probe that times out is a legacy verdict only on stdio
    (*timeout_means_legacy*); over HTTP it is an outage, not an era.
    """
    version = LATEST_MODERN
    for attempt in range(2):
        try:
            result = await request(
                "server/discover", {"_meta": modern_meta(version)}, probe_timeout
            )
        except McpRpcError as e:
            if e.code == UNSUPPORTED_PROTOCOL_VERSION:
                supported = _supported(e)
                mutual = _mutual_modern(supported)
                if mutual and attempt == 0:
                    version = mutual[-1]
                    continue
                if supported and not any(v in LEGACY_VERSIONS for v in supported):
                    raise _speaks_nothing_we_do(supported) from e
            elif e.code in MODERN_ERRORS:
                raise McpError(f"the server refused tstd's request: {e}") from e
        except NotModernError:
            pass
        except TimeoutError:
            if not timeout_means_legacy:
                raise
        else:
            modern = _discovered(result)
            if modern is not None:
                return modern
        try:
            return await _initialize(request, startup_timeout)
        except McpRpcError as e:
            # A dual-era server that answered the probe after we stopped
            # waiting is locked modern: it names its versions here.
            mutual = _mutual_modern(_supported(e))
            if e.code != UNSUPPORTED_PROTOCOL_VERSION or attempt != 0 or not mutual:
                raise
            version = mutual[-1]
    raise McpError("MCP version negotiation did not settle")


def _discovered(result: Any) -> Negotiated | None:
    """The modern agreement a ``DiscoverResult`` offers, if any."""
    if not isinstance(result, dict):
        return None
    raw = result.get("supportedVersions")
    versions = [v for v in raw if isinstance(v, str)] if isinstance(raw, list) else []
    mutual = _mutual_modern(versions)
    if not mutual:
        return None  # a discover-answering server that offers only legacy
    caps = result.get("capabilities")
    return Negotiated("modern", mutual[-1], dict(caps) if isinstance(caps, dict) else {})


async def _initialize(request: RawRequest, wait: float) -> Negotiated:
    result = await request("initialize", initialize_params(), wait)
    if not isinstance(result, dict):
        raise McpError("MCP initialize result was not an object")
    version = result.get("protocolVersion")
    if not isinstance(version, str) or version not in LEGACY_VERSIONS:
        raise _speaks_nothing_we_do([str(version)])
    caps = result.get("capabilities")
    return Negotiated("legacy", version, dict(caps) if isinstance(caps, dict) else {})
