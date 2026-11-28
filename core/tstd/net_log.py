"""One log line per outgoing HTTP request: method, host and path (LIVE2-01).

httpx logged every request with its full URL, so the daemon log held the
words of every web search, every fetched address with its share-link
parameters, and the addresses of the user's model servers. Its own logger
is capped at WARNING now (``setup_logging``); a client that wants its
requests in the log adds ``REQUEST_HOOKS`` and gets this line instead.
"""

from __future__ import annotations

from typing import Any

import httpx

from .logging import get_logger

log = get_logger("tstd.http")


def request_line(url: httpx.URL) -> str:
    """The scheme, host, port when given, and path: no user info, query or fragment."""
    port = f":{url.port}" if url.port is not None else ""
    return f"{url.scheme}://{url.host}{port}{url.path}"


async def log_request(request: httpx.Request) -> None:
    log.info(
        "http request",
        extra={"extra_fields": {"method": request.method, "url": request_line(request.url)}},
    )


REQUEST_HOOKS: dict[str, list[Any]] = {"request": [log_request]}
