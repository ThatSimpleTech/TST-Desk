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
    monkeypatch.setattr(search_mod, "_endpoint", lambda: ("", 5.0, 5, 200_000))
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

    monkeypatch.setattr(
        search_mod, "_endpoint", lambda: ("http://127.0.0.1:9/search", 5.0, 8, 200_000)
    )
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

    monkeypatch.setattr(
        search_mod, "_endpoint", lambda: ("http://127.0.0.1:9/search", 5.0, 8, 200_000)
    )
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", _client_factory(handler))
    out = await search_mod.web_search(None, "topic")
    assert "The Article" in out
    assert "http://127.0.0.1/article" in out
    assert "More" not in out


async def test_http_error_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="nope")

    monkeypatch.setattr(
        search_mod, "_endpoint", lambda: ("http://127.0.0.1:9/search", 5.0, 8, 200_000)
    )
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", _client_factory(handler))
    out = await search_mod.web_search(None, "topic")
    assert out.startswith("Error: search request failed")


async def test_fetch_empty_url() -> None:
    assert (await search_mod.web_fetch(None, "  ")).startswith("Error: url is empty")


def test_loopback_and_schemes_are_blocked() -> None:
    assert search_mod._blocked_reason("http://127.0.0.1/x") is not None
    assert search_mod._blocked_reason("http://localhost/x") is not None
    assert search_mod._blocked_reason("file:///etc/passwd") is not None
    assert search_mod._blocked_reason("http://169.254.169.254/") is not None


async def test_fetch_html_strips_script(monkeypatch: pytest.MonkeyPatch) -> None:
    html = "<html><script>secret()</script><p>Hello <b>world</b></p></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    monkeypatch.setattr(search_mod, "_blocked_reason", lambda _url: None)
    monkeypatch.setattr(search_mod, "_endpoint", lambda: ("", 5.0, 8, 200_000))
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", _client_factory(handler))
    out = await search_mod.web_fetch(None, "http://127.0.0.1/page")
    assert "Hello" in out
    assert "world" in out
    assert "secret" not in out


async def test_fetch_refuses_redirect_onto_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/go":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        return httpx.Response(200, text="leaked")

    def blocked(url: str) -> str | None:
        if "127.0.0.1" in url:
            return "that address cannot be fetched"
        return None

    monkeypatch.setattr(search_mod, "_blocked_reason", blocked)
    monkeypatch.setattr(search_mod, "_endpoint", lambda: ("", 5.0, 8, 200_000))
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", _client_factory(handler))
    out = await search_mod.web_fetch(None, "http://203.0.113.1/go")
    assert "cannot be fetched" in out
    assert "leaked" not in out
