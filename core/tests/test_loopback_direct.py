"""Loopback calls never ride a proxy (TD-5181).

httpx sends ``127.0.0.1`` and ``localhost`` through ``HTTP_PROXY`` /
``HTTPS_PROXY`` / ``ALL_PROXY`` unless ``NO_PROXY`` lists them, and the
proxy may be another machine. Every daemon client that can be pointed at a
server on this computer is driven here against a real local server while
the proxy variables name a recording fake proxy. The server must see the
request and the proxy must see nothing. A hosted URL must still go to the
proxy, so a person who needs one keeps it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tstd.autonomy.judgment import JudgmentKind, JudgmentQuestion
from tstd.autonomy.typesafe import TypeSafeJudgmentBackend
from tstd.config import (
    FastConfig,
    ModelConfig,
    NotifyConfig,
    NtfyNotifyConfig,
    Preset,
    SearchConfig,
    SpeechConfig,
    TierConfig,
    VoiceConfig,
)
from tstd.context.embeddings import EmbeddingsClient
from tstd.desktop.grounding_client import GroundingClient
from tstd.discovery import discover_model
from tstd.fast_switch import is_on
from tstd.model_setup import list_endpoint_models
from tstd.net import direct_transport, is_loopback_url
from tstd.notify.ntfy import send as ntfy_send
from tstd.provider import ProviderClient
from tstd.speech import transcribe
from tstd.tools.web_search import web_search
from tstd.voice import transcribe_audio

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
_COMPLETION = {
    "id": "c1",
    "object": "chat.completion",
    "created": 0,
    "model": "m1",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "(1, 2)"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def _body_for(path: str) -> dict[str, Any]:
    if path.endswith("/models"):
        return {"object": "list", "data": [{"id": "m1"}]}
    if path.endswith("/embeddings"):
        return {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}
    if path.endswith("/audio/transcriptions"):
        return {"text": "hello"}
    if path.endswith("/chat/completions"):
        return _COMPLETION
    if path.split("?")[0].endswith("/search"):
        return {"results": [{"title": "T", "url": "https://a.example/", "content": "c"}]}
    if path.endswith("/fast"):
        return {"on": True, "last": "test"}
    return {"answers": {}}


@dataclass
class _Listener:
    """A one-request-per-connection HTTP server that records request lines."""

    reply: Callable[[str], tuple[int, dict[str, Any]]]
    lines: list[str] = field(default_factory=list)
    port: int = 0
    _server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return
        text = head.decode("latin-1")
        line = text.split("\r\n", 1)[0]
        self.lines.append(line)
        length = 0
        for header in text.split("\r\n")[1:]:
            name, _, value = header.partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip())
        if length:
            await reader.readexactly(length)
        status, body = self.reply(line.split(" ")[1] if " " in line else "/")
        payload = json.dumps(body).encode()
        writer.write(
            f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
            + payload
        )
        await writer.drain()
        writer.close()


@pytest.fixture
async def server() -> AsyncIterator[_Listener]:
    listener = _Listener(reply=lambda path: (200, _body_for(path)))
    await listener.start()
    yield listener
    await listener.stop()


@pytest.fixture
async def proxy(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Listener]:
    """A fake proxy every proxy variable points at. It answers 502."""
    listener = _Listener(reply=lambda _path: (502, {"error": "proxy"}))
    await listener.start()
    for name in _PROXY_VARS:
        monkeypatch.setenv(name, f"http://127.0.0.1:{listener.port}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    yield listener
    await listener.stop()


def _tier(base_url: str) -> TierConfig:
    return TierConfig(
        slug="m1",
        base_url=base_url,
        input_price=0,
        output_price=0,
        cache_read_price=0,
        context_window=8192,
        max_output_tokens=64,
    )


def _config(base: str, **update: Any) -> ModelConfig:
    tier = _tier(f"{base}/v1")
    return ModelConfig(
        presets={"t": Preset(brain=tier, worker=tier, validator=tier)},
        active_preset="t",
        **update,
    )


async def _provider(base: str) -> None:
    client = ProviderClient(base_url=f"{base}/v1", api_key=None)
    try:
        await client._client.get(f"{base}/v1/models")
    finally:
        await client.close()


async def _discovery(base: str) -> None:
    assert await discover_model(f"{base}/v1") == "m1"


async def _model_list(base: str) -> None:
    reply = await list_endpoint_models(f"{base}/v1", None)
    assert reply.error is None


async def _embeddings(base: str) -> None:
    assert await EmbeddingsClient(f"{base}/v1", "e1", timeout_seconds=5).embed_or_none(["x"])


async def _voice(base: str) -> None:
    config = VoiceConfig(base_url=f"{base}/v1", timeout_seconds=5)
    assert await transcribe_audio(b"clip", "audio/webm", config) == "hello"


async def _speech(base: str) -> None:
    config = _config(base, speech=SpeechConfig(enabled=True, base_url=f"{base}/v1"))
    audio = base64.b64encode(b"clip").decode("ascii")
    assert (await transcribe(config, audio, "audio/webm")).ok


async def _grounding(base: str) -> None:
    await GroundingClient(f"{base}/v1", "m1", 5)._complete("m1", b"png", "the button", None, None)


async def _typesafe(base: str) -> None:
    backend = TypeSafeJudgmentBackend(base_url=base, api_key="k", timeout_seconds=5)
    question = JudgmentQuestion(
        kind=JudgmentKind.CHOICE,
        instructions="Pick one.",
        state=(("Tool", "fs_edit"),),
        options=("A", "B"),
        question_id="q",
    )
    # The fake reply carries no answer; only where the request went matters.
    with contextlib.suppress(ValueError):
        await backend._evaluate(question)


async def _fast_switch(base: str) -> None:
    assert await is_on(FastConfig(hook_url=f"{base}/fast", hook_model="m1")) == (True, "test")


async def _ntfy(base: str) -> None:
    config = _config(
        base,
        notify=NotifyConfig(
            ntfy=NtfyNotifyConfig(enabled=True, host="127.0.0.1", timeout_seconds=5)
        ),
    )
    await ntfy_send(config, "done", topic_url=f"{base}/topic")


async def _web_search(base: str) -> None:
    session = SimpleNamespace(
        config=SimpleNamespace(search=SearchConfig(base_url=f"{base}/search"))
    )
    assert "https://a.example/" in await web_search(session, "tst desk")


_CLIENTS: dict[str, Callable[[str], Awaitable[None]]] = {
    "provider": _provider,
    "discovery": _discovery,
    "model_list": _model_list,
    "embeddings": _embeddings,
    "voice": _voice,
    "speech": _speech,
    "grounding": _grounding,
    "typesafe": _typesafe,
    "fast_switch": _fast_switch,
    "ntfy": _ntfy,
    "web_search": _web_search,
}


@pytest.mark.parametrize("name", sorted(_CLIENTS))
async def test_loopback_call_skips_the_proxy(
    name: str, server: _Listener, proxy: _Listener
) -> None:
    await _CLIENTS[name](f"http://127.0.0.1:{server.port}")
    assert server.lines, f"{name} never reached the local server"
    assert proxy.lines == [], f"{name} sent a loopback request through the proxy"


async def test_localhost_name_skips_the_proxy(server: _Listener, proxy: _Listener) -> None:
    reply = await list_endpoint_models(f"http://localhost:{server.port}/v1", None)
    assert reply.error is None
    assert proxy.lines == []


async def test_hosted_url_still_uses_the_proxy(proxy: _Listener) -> None:
    """An address off this computer goes to the proxy, as the person set it up.

    The proxy answers 502, so the listing fails; what matters is where the
    request went. 192.0.2.1 is a documentation address nothing listens on.
    """
    reply = await list_endpoint_models("http://192.0.2.1:9/v1", None)
    assert reply.error is not None
    assert proxy.lines == ["GET http://192.0.2.1:9/v1/models HTTP/1.1"]


async def test_hosted_chat_client_keeps_the_proxy(proxy: _Listener) -> None:
    client = ProviderClient(base_url="http://192.0.2.1:9/v1", api_key=None)
    try:
        response = await client._client.get("http://192.0.2.1:9/v1/models")
    finally:
        await client.close()
    assert response.status_code == 502
    assert proxy.lines == ["GET http://192.0.2.1:9/v1/models HTTP/1.1"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/v1",
        "http://127.8.9.10:1/v1",
        "http://localhost:1234/v1",
        "http://[::1]:8000/v1",
    ],
)
def test_direct_transport_for_this_computer(url: str) -> None:
    assert is_loopback_url(url)
    assert isinstance(direct_transport(url), httpx.AsyncHTTPTransport)


@pytest.mark.parametrize(
    "url",
    [
        "https://openrouter.ai/api/v1",
        "http://10.0.0.5:11434/v1",
        "http://localhost.example.com/v1",
        "127.0.0.1:11434/v1",
        "",
    ],
)
def test_no_transport_off_this_computer(url: str) -> None:
    assert direct_transport(url) is None
