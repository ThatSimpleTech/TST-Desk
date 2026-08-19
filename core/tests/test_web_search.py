"""Web search tool (TD-609). Destination comes from config, never source."""

from __future__ import annotations

import httpx
import pytest

import tstd.tools.web_search as search_mod

_RealAsyncClient = httpx.AsyncClient


def _client_factory(handler: object) -> object:
    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)  # type: ignore[arg-type]
        return _RealAsyncClient(*args, **kwargs)  # type: ignore[arg-type]

    return factory


async def test_empty_query_is_refused() -> None:
    assert (await search_mod.web_search(None, "   ")).startswith("Error: query is empty")


async def test_blank_endpoint_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_mod, "_endpoint", lambda: ("", 5.0, 5))
    out = await search_mod.web_search(None, "anything")
    assert "not configured" in out
    assert "search.base_url" in out


async def test_json_results_follow_config_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "One",
                        "url": "http://127.0.0.1/one",
                        "content": "first hit",
                    },
                    {
                        "title": "Two",
                        "href": "http://127.0.0.1/two",
                        "snippet": "second",
                    },
                ]
            },
        )

    monkeypatch.setattr(search_mod, "_endpoint", lambda: ("http://127.0.0.1:9/search", 5.0, 8))
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", _client_factory(handler))
    out = await search_mod.web_search(None, "tst desk", max_results=2)
    assert "q=tst+desk" in seen[0] or "q=tst%20desk" in seen[0]
    assert "1. One" in out
    assert "http://127.0.0.1/one" in out
    assert "first hit" in out
    assert "2. Two" in out


async def test_html_results_skip_the_search_host(monkeypatch: pytest.MonkeyPatch) -> None:
    html = """
    <a href="http://127.0.0.1:9/next">More</a>
    <a href="http://127.0.0.1/article">The Article</a>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    monkeypatch.setattr(search_mod, "_endpoint", lambda: ("http://127.0.0.1:9/search", 5.0, 8))
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", _client_factory(handler))
    out = await search_mod.web_search(None, "topic")
    assert "The Article" in out
    assert "http://127.0.0.1/article" in out
    assert "More" not in out


async def test_http_error_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="nope")

    monkeypatch.setattr(search_mod, "_endpoint", lambda: ("http://127.0.0.1:9/search", 5.0, 8))
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", _client_factory(handler))
    out = await search_mod.web_search(None, "topic")
    assert out.startswith("Error: search request failed")
