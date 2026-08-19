"""Web search tool (TD-609).

The destination is always ``search.base_url`` from config — never a host
written into this file. An empty base_url disables the tool. The classifier
sees no ``host_fields``, so a call is Class B (ask) unless policy says
otherwise.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ..config import cached_config
from ..logging import redact_secrets

_MAX_BODY = 512_000
_SCHEMES = ("http://", "https://")


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


def _endpoint() -> tuple[str, float, int]:
    cfg = cached_config().search
    return cfg.base_url.strip(), cfg.timeout_seconds, cfg.max_results


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
    base, timeout, default_limit = _endpoint()
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
