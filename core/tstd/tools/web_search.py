"""Web search and page fetch (TD-609, TD-610).

``web_search`` hits only ``search.base_url`` from config. ``web_fetch``
takes a URL the model chose (a search hit); that call is Class B so the
user sees the address. Loopback, link-local, and metadata addresses are
refused in the handler — that is the wall, not an internet allowlist.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ..config import cached_config
from ..logging import redact_secrets

_MAX_BODY = 512_000
_SCHEMES = ("http://", "https://")
_MAX_REDIRECTS = 5
_BLOCKED_NAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "metadata.google.internal",
    }
)


def search_hosts() -> tuple[str, ...]:
    """The hosts ``web_search`` reaches, from config — for the classifier.

    The search endpoint is ``search.base_url``, not a tool argument, so
    the dispatcher cannot read it out of the call.  Registered as the
    tool's ``host_resolver`` (TD-4808): ``network: deny`` and the host
    allowlist classify the call before it runs.  Empty when search is
    not configured or the URL has no host.
    """
    base_url = cached_config().search.base_url.strip()
    if not base_url:
        return ()
    host = urlsplit(base_url).hostname
    return (host.lower(),) if host else ()


class _AnchorCollector(HTMLParser):
    """Collect http(s) anchors and their link text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href: str | None = None
        self._parts: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href and href.startswith(_SCHEMES):
            self._href = href
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._href is None:
            return
        title = " ".join(part.strip() for part in self._parts if part.strip())
        self.links.append((title or self._href, self._href))
        self._href = None


class _TextExtractor(HTMLParser):
    """Visible text only — skip script/style so a page is readable."""

    _SKIP = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip == 0:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _endpoint() -> tuple[str, float, int, int]:
    cfg = cached_config().search
    return cfg.base_url.strip(), cfg.timeout_seconds, cfg.max_results, cfg.fetch_max_bytes


def _blocked_reason(url: str) -> str | None:
    """Why *url* must not be fetched, or None if it may.

    Loopback, link-local, unspecified, and multicast addresses are the
    wall: a fetch of those is the machine, not the public web. Public
    (and LAN) http(s) URLs are Class B — the user sees the address.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "only http and https URLs can be fetched"
    host = parts.hostname
    if host is None or host == "":
        return "URL has no host"
    if host.lower() in _BLOCKED_NAMES:
        return "that host cannot be fetched"
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    try:
        candidates.append(ipaddress.ip_address(host))
    except ValueError:
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            return f"could not resolve host ({exc})"
        for info in infos:
            addr = info[4][0]
            try:
                candidates.append(ipaddress.ip_address(addr))
            except ValueError:
                continue
    if not candidates:
        return "could not resolve host"
    for ip in candidates:
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            return "that address cannot be fetched"
    return None


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


def _from_json(payload: Any, limit: int) -> list[tuple[str, str, str]]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get("results")
    if raw is None and isinstance(payload.get("web"), dict):
        raw = payload["web"].get("results")
    if raw is None:
        raw = payload.get("items")
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("href") or item.get("link")
        title = item.get("title") or item.get("name") or ""
        snippet = item.get("content") or item.get("snippet") or item.get("description") or ""
        if isinstance(url, str) and url.startswith(_SCHEMES):
            out.append((str(title), url, str(snippet)))
        if len(out) >= limit:
            break
    return out


def _from_html(html: str, base: str, limit: int) -> list[tuple[str, str, str]]:
    parser = _AnchorCollector()
    parser.feed(html)
    own = urlsplit(base).netloc
    seen: set[str] = set()
    out: list[tuple[str, str, str]] = []
    for title, href in parser.links:
        url = urljoin(base, href)
        host = urlsplit(url).netloc
        if not host or host == own or url in seen:
            continue
        seen.add(url)
        out.append((title, url, ""))
        if len(out) >= limit:
            break
    return out


def _render(rows: list[tuple[str, str, str]]) -> str:
    if not rows:
        return "No search results."
    lines: list[str] = []
    for i, (title, url, snippet) in enumerate(rows, start=1):
        lines.append(f"{i}. {title}\n   {url}")
        if snippet.strip():
            lines.append(f"   {snippet.strip()}")
    return redact_secrets("\n".join(lines))


async def web_search(
    session: object,
    query: str,
    max_results: int = 0,
    tool_call_id: str = "",
) -> str:
    """Search the public web. Destination is config, not the query."""
    text = query.strip()
    if text == "":
        return "Error: query is empty"
    base, timeout, default_limit, _cap = _endpoint()
    if base == "":
        return (
            "Error: web search is not configured. Set search.base_url in "
            "config.yaml (user data dir) to a search endpoint, then restart."
        )
    limit = max_results if max_results and max_results > 0 else default_limit
    limit = max(1, min(limit, 20))
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(base, params={"q": text})
            response.raise_for_status()
            body = response.text[:_MAX_BODY]
            ctype = response.headers.get("content-type", "")
    except httpx.HTTPError as exc:
        return f"Error: search request failed ({exc})"

    rows: list[tuple[str, str, str]] = []
    if "json" in ctype or body.lstrip().startswith(("{", "[")):
        try:
            rows = _from_json(json.loads(body), limit)
        except json.JSONDecodeError:
            rows = []
    if not rows:
        rows = _from_html(body, str(response.url), limit)
    return _render(rows)


async def web_fetch(
    session: object,
    url: str,
    tool_call_id: str = "",
) -> str:
    """Fetch one page and return readable text (TD-610)."""
    target = url.strip()
    if target == "":
        return "Error: url is empty"
    blocked = _blocked_reason(target)
    if blocked is not None:
        return f"Error: {blocked}"
    _base, timeout, _limit, cap = _endpoint()
    current = target
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response: httpx.Response | None = None
            for _ in range(_MAX_REDIRECTS):
                blocked = _blocked_reason(current)
                if blocked is not None:
                    return f"Error: {blocked}"
                response = await client.get(current)
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return "Error: redirect with no location"
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                break
            else:
                return "Error: too many redirects"
            assert response is not None
            raw = response.text[: cap + 1]
            ctype = response.headers.get("content-type", "")
            final = str(response.url)
    except httpx.HTTPError as exc:
        return f"Error: fetch failed ({exc})"

    blocked = _blocked_reason(final)
    if blocked is not None:
        return f"Error: {blocked}"
    body = _html_to_text(raw) if "html" in ctype else raw
    body = redact_secrets(body)
    if len(raw) > cap:
        body = body[:cap] + (
            "\n\n… [truncated: page exceeds cap; web_fetch a more specific URL "
            "or web_search a narrower query]"
        )
    return body
