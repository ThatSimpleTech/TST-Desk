"""'Find models on this computer' (TD-5180).

The scan asks each configured loopback server for ``/models`` and offers the
ones that list models. These tests pin the rules from the module docstring:
loopback only, no key, proxies ignored, a server that answers with something
other than a model list is not offered, and a slow one cannot stall the rest.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic import ValidationError

from tests.test_add_model import _ask, _start, _stop
from tstd import local_servers
from tstd.config import LocalServerConfig, ModelConfig, default_config_yaml, load_config
from tstd.local_servers import scan_local_servers
from tstd.mock import MockProvider, Script
from tstd.protocol import LocalServers, ScanLocalServers, parse_client_message

_MODELS = {"object": "list", "data": [{"id": "qwen3:8b"}, {"id": "llama3.2:3b"}]}


def _servers(*pairs: tuple[str, str]) -> list[LocalServerConfig]:
    return [LocalServerConfig(name=name, base_url=url) for name, url in pairs]


# ── Config ───────────────────────────────────────────────────────────────


class TestConfig:
    def test_shipped_config_lists_the_usual_local_servers(self) -> None:
        config = ModelConfig.model_validate(yaml.safe_load(default_config_yaml()))
        assert [s.name for s in config.local_servers] == [
            "Ollama",
            "LM Studio",
            "vLLM",
            "llama.cpp",
        ]
        ports = [httpx.URL(s.base_url).port for s in config.local_servers]
        assert ports == [11434, 1234, 8000, 8080]

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.example.com/v1",
            "http://192.168.1.20:11434/v1",
            "http://localhost.example.com:11434/v1",
            "127.0.0.1:11434/v1",
            "ftp://127.0.0.1/v1",
        ],
    )
    def test_an_off_box_or_schemeless_url_fails_the_load(self, url: str) -> None:
        with pytest.raises(ValidationError, match="must be on this computer"):
            LocalServerConfig(name="X", base_url=url)

    @pytest.mark.parametrize(
        "url", ["http://127.0.0.1:11434/v1/", "http://localhost:1234/v1", "http://[::1]:8080/v1"]
    )
    def test_loopback_urls_load_without_a_trailing_slash(self, url: str) -> None:
        assert LocalServerConfig(name="X", base_url=url).base_url == url.rstrip("/")

    def test_an_older_config_gets_the_shipped_list(self, tmp_path: Path) -> None:
        # A user copy written before TD-5180 has no local_servers key.
        path = tmp_path / "config.yaml"
        path.write_text("presets: {}\n", encoding="utf-8")
        assert [s.name for s in load_config(path).local_servers] == [
            "Ollama",
            "LM Studio",
            "vLLM",
            "llama.cpp",
        ]

    def test_an_explicit_empty_list_stays_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("presets: {}\nlocal_servers: []\n", encoding="utf-8")
        assert load_config(path).local_servers == []

    def test_an_empty_section_turns_the_search_off(self) -> None:
        assert ModelConfig.model_validate({"local_servers": None}).local_servers == []
        assert ModelConfig.model_validate({"local_servers": []}).local_servers == []

    def test_the_message_parses(self) -> None:
        assert isinstance(parse_client_message('{"type": "scan_local_servers"}'), ScanLocalServers)


# ── The scan, against a scripted transport ───────────────────────────────


class Recorder:
    """A transport that answers per port and records every request."""

    def __init__(self, by_port: dict[int, Any]) -> None:
        self.by_port = by_port
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.by_port.get(request.url.port or 0)
        if answer is None:
            raise httpx.ConnectError("connection refused", request=request)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, str):
            return httpx.Response(200, text=answer, request=request)
        return httpx.Response(200, json=answer, request=request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class TestScan:
    async def test_only_servers_that_list_models_are_offered(self) -> None:
        rec = Recorder(
            {
                11434: _MODELS,
                1234: "<html>a web app, not a model server</html>",
                8000: {"data": []},
                8080: {"object": "list", "data": [{"id": "gguf-model"}]},
            }
        )
        servers = _servers(
            ("Ollama", "http://127.0.0.1:11434/v1"),
            ("LM Studio", "http://127.0.0.1:1234/v1"),
            ("vLLM", "http://127.0.0.1:8000/v1"),
            ("llama.cpp", "http://127.0.0.1:8080/v1"),
            ("Nothing here", "http://127.0.0.1:9999/v1"),
        )
        async with rec.client() as client:
            found = await scan_local_servers(servers, client=client)
        assert [(s.name, s.base_url) for s in found.servers] == [
            ("Ollama", "http://127.0.0.1:11434/v1"),
            ("llama.cpp", "http://127.0.0.1:8080/v1"),
        ]
        assert [m.id for m in found.servers[0].models] == ["llama3.2:3b", "qwen3:8b"]
        assert found.checked == [s.base_url for s in servers]

    async def test_no_key_is_sent(self) -> None:
        rec = Recorder({11434: _MODELS})
        async with rec.client() as client:
            await scan_local_servers(
                _servers(("Ollama", "http://127.0.0.1:11434/v1")), client=client
            )
        assert rec.requests
        assert all("authorization" not in r.headers for r in rec.requests)
        assert [str(r.url) for r in rec.requests] == ["http://127.0.0.1:11434/v1/models"]

    async def test_a_url_listed_twice_is_asked_once(self) -> None:
        rec = Recorder({11434: _MODELS})
        servers = _servers(
            ("Ollama", "http://127.0.0.1:11434/v1"),
            ("Ollama again", "http://127.0.0.1:11434/v1/"),
        )
        async with rec.client() as client:
            found = await scan_local_servers(servers, client=client)
        assert len(rec.requests) == 1
        assert [s.name for s in found.servers] == ["Ollama"]
        assert found.checked == ["http://127.0.0.1:11434/v1"]

    async def test_an_off_box_entry_is_never_asked(self) -> None:
        # The config refuses one; a caller that builds the entry by hand
        # past the validator still cannot reach off the machine.
        rogue = LocalServerConfig.model_construct(name="Rogue", base_url="https://example.com/v1")
        rec = Recorder({})
        async with rec.client() as client:
            found = await scan_local_servers([rogue], client=client)
        assert rec.requests == []
        assert found == LocalServers()

    async def test_nothing_listed_means_nothing_asked(self) -> None:
        found = await scan_local_servers([])
        assert found.servers == [] and found.checked == []

    async def test_a_timeout_is_a_miss_not_an_error(self) -> None:
        rec = Recorder({11434: httpx.ReadTimeout("slow")})
        async with rec.client() as client:
            found = await scan_local_servers(
                _servers(("Ollama", "http://127.0.0.1:11434/v1")), client=client
            )
        assert found.servers == []
        assert found.checked == ["http://127.0.0.1:11434/v1"]


# ── The scan, against real sockets ───────────────────────────────────────


class _ModelsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps(_MODELS).encode()
        self.send_response(200 if self.path == "/v1/models" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def models_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def silent_listener() -> Iterator[str]:
    """Accepts connections and never answers."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/v1"
    finally:
        sock.close()


class TestRealSockets:
    async def test_a_proxy_setting_does_not_reroute_the_scan(
        self, models_server: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A proxy nothing listens on: if the scan honoured it, it would fail.
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
            monkeypatch.setenv(var, "http://127.0.0.1:9")
        for var in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(var, raising=False)
        found = await scan_local_servers(_servers(("Ollama", models_server)))
        assert [s.base_url for s in found.servers] == [models_server]

    async def test_a_silent_server_cannot_stall_the_others(
        self, models_server: str, silent_listener: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(local_servers, "SCAN_TIMEOUT", 0.3)
        started = time.monotonic()
        found = await scan_local_servers(
            _servers(("Silent", silent_listener), ("Ollama", models_server))
        )
        assert time.monotonic() - started < 2.0
        assert [s.name for s in found.servers] == ["Ollama"]
        assert found.checked == [silent_listener, models_server]


# ── Through the daemon ───────────────────────────────────────────────────


@pytest.fixture
def data_dir(tmp_path: Path, models_server: str) -> Path:
    """A data dir whose config lists the live models server and a dead port."""
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "presets": {},
                "local_servers": [
                    {"name": "Ollama", "base_url": models_server},
                    {"name": "LM Studio", "base_url": "http://127.0.0.1:9/v1"},
                ],
            }
        )
    )
    return tmp_path


class TestDaemon:
    async def test_scan_local_servers_round_trip(self, data_dir: Path, models_server: str) -> None:
        _daemon, task, ws = await _start(data_dir, MockProvider(default=Script(kind="text")))
        try:
            reply = await asyncio.wait_for(
                _ask(ws, {"type": "scan_local_servers"}, "local_servers"), 15
            )
        finally:
            await _stop(task, ws)
        assert reply["type"] == "local_servers"
        assert [s["name"] for s in reply["servers"]] == ["Ollama"]
        assert {m["id"] for m in reply["servers"][0]["models"]} == {"qwen3:8b", "llama3.2:3b"}
        assert reply["checked"] == [models_server, "http://127.0.0.1:9/v1"]
