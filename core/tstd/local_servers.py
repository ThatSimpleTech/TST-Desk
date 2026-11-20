"""'Find models on this computer' in the "Add a model" walkthrough (TD-5180).

Asks every loopback server in the config's ``local_servers`` for its
``/models`` at once, with a short timeout, and keeps the ones that answer
with at least one model. Three rules keep it quiet and on the machine:

* **User-initiated.** The walkthrough sends ``scan_local_servers`` only when
  the user clicks the button (§2.3); nothing here runs on its own.
* **Loopback only, no key.** The config refuses an off-box entry and this
  module skips one again. The client ignores proxy settings and, like every
  httpx client, follows no redirects, so a request cannot leave the machine.
  No credential is ever sent.
* **Nothing is written.** The reply only lists what answered. The user picks
  a model and the walkthrough's normal ``add_model`` step saves it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import httpx

from .config import LocalServerConfig, is_loopback_url
from .model_setup import list_endpoint_models
from .net import direct_transport
from .protocol import LocalServer, LocalServers

SCAN_TIMEOUT = 1.5
"""Seconds per server. A closed port fails at once; this bounds a program
that accepts the connection and never answers."""


def _unique_loopback(servers: Sequence[LocalServerConfig]) -> list[tuple[str, str]]:
    """``(name, url)`` per distinct loopback URL, in config order."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for server in servers:
        url = server.base_url.strip().rstrip("/")
        if url in seen or not is_loopback_url(url):
            continue
        seen.add(url)
        out.append((server.name, url))
    return out


async def scan_local_servers(
    servers: Sequence[LocalServerConfig],
    *,
    client: httpx.AsyncClient | None = None,
) -> LocalServers:
    """Ask each listed server for ``/models`` and keep those that list models."""
    wanted = _unique_loopback(servers)
    if not wanted:
        return LocalServers()
    owned = client is None
    # Every URL here is loopback, so the transport skips any proxy setting
    # (TD-5181): a proxy must not carry a loopback request to another machine.
    http = client or httpx.AsyncClient(
        timeout=SCAN_TIMEOUT, transport=direct_transport(wanted[0][1])
    )
    try:
        replies = await asyncio.gather(
            *(
                list_endpoint_models(url, None, client=http, ask_gateway=False)
                for _name, url in wanted
            )
        )
    finally:
        if owned:
            await http.aclose()
    found = [
        LocalServer(name=name, base_url=reply.base_url, models=reply.models)
        for (name, _url), reply in zip(wanted, replies, strict=True)
        if reply.error is None and reply.models
    ]
    return LocalServers(servers=found, checked=[url for _name, url in wanted])


async def loopback_answers(
    urls: Sequence[str], *, client: httpx.AsyncClient | None = None
) -> dict[str, bool]:
    """Whether a server answers at each loopback URL (TD-5240).

    The picker's readiness check for a model served on this computer. The
    same three rules as the scan above: asked only when the person opens
    the picker, loopback only with no key (an off-box URL is not asked and
    reads as answering: it is not this function's to judge), and nothing
    written. Any HTTP reply counts, a refused connection or a timeout does
    not: this says a server is there, not that the model will run.
    """
    wanted = list(dict.fromkeys(url.strip().rstrip("/") for url in urls))
    local = [url for url in wanted if is_loopback_url(url)]
    found = {url: True for url in wanted}
    if not local:
        return found
    owned = client is None
    http = client or httpx.AsyncClient(timeout=SCAN_TIMEOUT, transport=direct_transport(local[0]))

    async def _ask(url: str) -> bool:
        try:
            await http.get(f"{url}/models", headers={"Accept": "application/json"})
        except httpx.HTTPError:
            return False
        return True

    try:
        answers = await asyncio.gather(*(_ask(url) for url in local))
    finally:
        if owned:
            await http.aclose()
    found.update(dict(zip(local, answers, strict=True)))
    return found
