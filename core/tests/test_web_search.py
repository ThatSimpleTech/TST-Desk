"""Web search tool (TD-609). Destination comes from config, never source."""

from __future__ import annotations

import base64
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

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
    monkeypatch.setattr(search_mod, "_search_urls", lambda: [])
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
    monkeypatch.setattr(search_mod, "_search_urls", lambda: ["http://127.0.0.1:9/search"])
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
    monkeypatch.setattr(search_mod, "_search_urls", lambda: ["http://127.0.0.1:9/search"])
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
    monkeypatch.setattr(search_mod, "_search_urls", lambda: ["http://127.0.0.1:9/search"])
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


# --- The connect-time wall (TD-4814) ---------------------------------------
#
# The pre-check resolves the host and refuses private answers, but before
# TD-4814 httpx resolved it again when opening the socket, and that second
# answer was free to differ — the classic DNS-rebinding TOCTOU. These tests
# stage the attack for real: a resolver that lies (clean answer first,
# loopback after) and an actual HTTP server on loopback counting every hit.


class _FlipResolver:
    """A DNS rebinding script in miniature: public first answer, loopback after."""

    def __init__(self, first: str, then: str) -> None:
        self._first = first
        self._then = then
        self.calls = 0

    async def __call__(self, host: str, port: int) -> list[tuple[object, ...]]:
        self.calls += 1
        ip = self._first if self.calls == 1 else self._then
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


def _rogue_server() -> tuple[HTTPServer, list[int]]:
    """The attacker's page: a real HTTP server on loopback, counting hits."""
    hits = [0]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits[0] += 1
            self.send_response(200)
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"pwned")

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, hits


def _clean_lie(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard's own resolution always sees a clean public address."""
    monkeypatch.setattr(
        search_mod.socket,
        "getaddrinfo",
        lambda host, port, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", port))
        ],
    )


async def test_double_flip_dns_never_reaches_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Public answer to the guard, loopback to the socket: refused, zero hits.

    With the fix the guarded backend's resolution is the only one — so the
    flip lands ON it and the private second answer is refused before any
    socket exists.
    """
    server, hits = _rogue_server()
    try:
        _clean_lie(monkeypatch)
        flip = _FlipResolver("203.0.113.7", "127.0.0.1")
        transport = search_mod._PinnedTransport(flip)
        monkeypatch.setattr(search_mod, "_pinned_transport", lambda: transport)
        monkeypatch.setattr(search_mod, "_endpoint", lambda: ("", 0.5, 5, 200_000))

        out = await search_mod.web_fetch(None, "http://rebind.example/")

        assert hits[0] == 0, "the rebinding fetch must never reach the loopback server"
        assert out.startswith("Error:"), out
        assert flip.calls == 1, "the guarded backend must be the only resolver"
    finally:
        server.shutdown()


async def test_private_answer_at_connect_time_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a first-answer-private lie dies at the wall it cannot bypass.

    The pre-check is fed the same clean lie as above; the refusal here is
    the connect-time half acting alone.
    """
    server, hits = _rogue_server()
    try:
        _clean_lie(monkeypatch)
        loopback_only = _FlipResolver("127.0.0.1", "127.0.0.1")
        monkeypatch.setattr(
            search_mod, "_pinned_transport", lambda: search_mod._PinnedTransport(loopback_only)
        )
        monkeypatch.setattr(search_mod, "_endpoint", lambda: ("", 0.5, 5, 200_000))

        out = await search_mod.web_fetch(None, "http://rebind.example/")

        assert "cannot be fetched" in out
        assert hits[0] == 0, "a private answer must not become a socket"
    finally:
        server.shutdown()


# --- Fallback chain ------------------------------------------------------
#
# The primary endpoint is tried first, then each fallback in order. A URL
# that fails the request — or answers with nothing parseable, the way a
# captcha page does — yields to the next one.

_PRIMARY = "http://127.0.0.1:9/search"
_FALLBACK = "http://127.0.0.1:10/search"
_CAPTCHA_HTML = "<html><title>Captcha</title></html>"


def _chain(monkeypatch: pytest.MonkeyPatch, urls: list[str]) -> None:
    monkeypatch.setattr(search_mod, "_endpoint", lambda: ("", 5.0, 8, 200_000))
    monkeypatch.setattr(search_mod, "_search_urls", lambda: list(urls))


def _results_json(*titles: str) -> dict[str, object]:
    return {
        "results": [
            {
                "title": title,
                "url": f"http://127.0.0.1/{title.lower()}",
                "content": f"about {title}",
            }
            for title in titles
        ]
    }


async def test_fallback_answers_when_primary_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "127.0.0.1:9" in str(request.url):
            return httpx.Response(502, text="nope")
        return httpx.Response(200, json=_results_json("Fallback"))

    _chain(monkeypatch, [_PRIMARY, _FALLBACK])
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", _client_factory(handler))
    out = await search_mod.web_search(None, "topic")
    assert "1. Fallback" in out
    assert len(seen) == 2


async def test_primary_success_never_touches_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_results_json("Primary"))

    _chain(monkeypatch, [_PRIMARY, _FALLBACK])
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", _client_factory(handler))
    out = await search_mod.web_search(None, "topic")
    assert "1. Primary" in out
    assert len(seen) == 1


async def test_unparseable_primary_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 with zero rows (captcha/bot page) is a failure, not an answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "127.0.0.1:9" in str(request.url):
            return httpx.Response(200, text=_CAPTCHA_HTML, headers={"content-type": "text/html"})
        return httpx.Response(200, json=_results_json("Fallback"))

    _chain(monkeypatch, [_PRIMARY, _FALLBACK])
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", _client_factory(handler))
    out = await search_mod.web_search(None, "topic")
    assert "1. Fallback" in out


async def test_all_endpoints_failing_names_each(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "127.0.0.1:9" in str(request.url):
            raise httpx.ConnectError("")
        return httpx.Response(502, text="nope")

    _chain(monkeypatch, [_PRIMARY, _FALLBACK])
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", _client_factory(handler))
    out = await search_mod.web_search(None, "topic")
    assert out.startswith("Error: search failed on 2 endpoint(s): ")
    assert _PRIMARY in out
    assert _FALLBACK in out
    # The bare ConnectError carries no message — the type is the diagnosis.
    assert "ConnectError" in out


async def test_bare_transport_error_names_the_exception_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a reset connection used to surface as `failed ()`."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("")

    _chain(monkeypatch, [_PRIMARY])
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", _client_factory(handler))
    out = await search_mod.web_search(None, "topic")
    assert out == "Error: search request failed (ConnectError)"


async def test_every_backend_empty_is_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_CAPTCHA_HTML, headers={"content-type": "text/html"})

    _chain(monkeypatch, [_PRIMARY, _FALLBACK])
    monkeypatch.setattr(search_mod.httpx, "AsyncClient", _client_factory(handler))
    assert (await search_mod.web_search(None, "topic")) == "No search results."


def _redirect_html(target: str) -> str:
    """A same-host redirect wrapper in the shape backends serve (Bing /ck/a)."""
    token = "a1" + base64.urlsafe_b64encode(target.encode()).decode()
    return f'<a href="http://127.0.0.1:9/ck/a?u={token}&ntb=1">The Article</a>'


def test_redirect_wrapper_links_unwrap_to_the_target() -> None:
    rows = search_mod._from_html(
        _redirect_html("http://127.0.0.1/article"), "http://127.0.0.1:9/search", 8
    )
    assert rows == [("The Article", "http://127.0.0.1/article", "")]


def test_unwrappable_same_host_links_stay_skipped() -> None:
    html = '<a href="http://127.0.0.1:9/more?q=x">More</a>'
    assert search_mod._from_html(html, "http://127.0.0.1:9/search", 8) == []


def test_unwrap_rejects_non_url_decodings() -> None:
    token = base64.urlsafe_b64encode(b"not a url").decode()
    assert search_mod._unwrap_redirect(f"http://127.0.0.1:9/ck/a?u={token}") is None
    assert search_mod._unwrap_redirect("http://127.0.0.1:9/ck/a") is None


def _fake_config(monkeypatch: pytest.MonkeyPatch, base: str, fallbacks: list[str]) -> None:
    search = SimpleNamespace(base_url=base, fallback_base_urls=fallbacks)
    monkeypatch.setattr(search_mod, "cached_config", lambda: SimpleNamespace(search=search))


def test_search_hosts_lists_the_whole_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_config(
        monkeypatch,
        "https://primary.example/s",
        ["https://backup.example/s", "", "https://primary.example/other"],
    )
    assert search_mod.search_hosts() == ("primary.example", "backup.example")


def test_search_hosts_empty_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_config(monkeypatch, "", [])
    assert search_mod.search_hosts() == ()


def test_search_urls_drops_blanks(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_config(monkeypatch, "", ["", "https://backup.example/s"])
    assert search_mod._search_urls() == ["https://backup.example/s"]
