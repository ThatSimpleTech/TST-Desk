"""Which MCP URLs tstd may dial (TD-4401, TD-5136).

Kept apart from the HTTP client so the check imports no network library.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from ..config import is_loopback_url
from .errors import McpError

__all__ = ["assert_http_url"]

_HTTP_FIX = (
    "Set mcp.servers.<id>.url to a loopback http(s) URL (127.0.0.1, ::1, or localhost), "
    "or to an https URL with allow_remote: true on that server."
)


def assert_http_url(url: str, *, allow_remote: bool = False) -> None:
    """Refuse a URL we must never dial.

    Destination is always ``server.url`` from config. Off-box needs the
    server's own ``allow_remote`` opt-in (TD-5136) and https, since its
    headers may carry a keychain token; without both it is a load-time
    refusal, not a request.
    """
    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise McpError(f"MCP url is not a URL: {e}", fix=_HTTP_FIX) from e
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise McpError("MCP url must be an http(s) endpoint", fix=_HTTP_FIX)
    if is_loopback_url(url):
        return
    if not allow_remote:
        raise McpError(
            "MCP url must be loopback; off-box destinations need allow_remote: true",
            fix=_HTTP_FIX,
        )
    if parts.scheme != "https":
        raise McpError("an off-box MCP url must be https", fix=_HTTP_FIX)
