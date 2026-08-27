"""Hold-to-talk speech-to-text (TD-4701).

The destination is ``speech.base_url`` from config. There is no cloud
default. Failures return a short ``detail`` code and never include the URL.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest

from tstd.config import ModelConfig, NotifyConfig, Preset, SpeechConfig, TierConfig
from tstd.keychain import KeychainError
from tstd.speech import MAX_AUDIO_BYTES, transcribe

LOCAL_STT = "http://127.0.0.1:64114/v1"
AUDIO_B64 = base64.b64encode(b"fake-opus-bytes").decode("ascii")


def _config(**speech: Any) -> ModelConfig:
    tier = TierConfig(
        slug="sentinel-model",
        base_url="http://127.0.0.1:64110/v1",
        input_price=0,
        output_price=0,
        cache_read_price=0,
        context_window=1024,
        max_output_tokens=16,
    )
    return ModelConfig(
        presets={"sentinel": Preset(brain=tier, worker=tier, validator=tier)},
        active_preset="sentinel",
        notify=NotifyConfig(),
        speech=SpeechConfig(**speech),
    )


class _ScriptedTransport(httpx.AsyncBaseTransport):
    def __init__(self, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {"text": "hello from the mic"}
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body, request=request)


@pytest.mark.asyncio
async def test_disabled_does_not_send() -> None:
    transport = _ScriptedTransport()
    result = await transcribe(
        _config(enabled=False, base_url=LOCAL_STT),
        AUDIO_B64,
        "audio/webm",
        transport=transport,
    )
    assert result.ok is False
    assert result.detail == "speech_disabled"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_unconfigured_does_not_send() -> None:
    transport = _ScriptedTransport()
    result = await transcribe(
        _config(enabled=True, base_url=""),
        AUDIO_B64,
        "audio/webm",
        transport=transport,
    )
    assert result.ok is False
    assert result.detail == "speech_unconfigured"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_bad_base64_is_typed() -> None:
    result = await transcribe(
        _config(enabled=True, base_url=LOCAL_STT),
        "not-valid-b64!!!",
        "audio/webm",
    )
    assert result.ok is False
    assert result.detail == "speech_bad_audio"


@pytest.mark.asyncio
async def test_unknown_mime_is_typed() -> None:
    result = await transcribe(
        _config(enabled=True, base_url=LOCAL_STT),
        AUDIO_B64,
        "application/octet-stream",
    )
    assert result.ok is False
    assert result.detail == "speech_bad_audio"


@pytest.mark.asyncio
async def test_too_large_is_typed() -> None:
    huge = base64.b64encode(b"x" * (MAX_AUDIO_BYTES + 1)).decode("ascii")
    result = await transcribe(
        _config(enabled=True, base_url=LOCAL_STT),
        huge,
        "audio/webm",
    )
    assert result.ok is False
    assert result.detail == "speech_too_large"


@pytest.mark.asyncio
async def test_missing_credential_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _missing(_name: str = "openrouter") -> str:
        raise KeychainError("missing")

    monkeypatch.setattr("tstd.speech.get_api_key", _missing)
    transport = _ScriptedTransport()
    result = await transcribe(
        _config(enabled=True, base_url=LOCAL_STT, credential="whisper"),
        AUDIO_B64,
        "audio/webm",
        transport=transport,
    )
    assert result.ok is False
    assert result.detail == "speech_auth"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_success_posts_multipart_without_model() -> None:
    transport = _ScriptedTransport()
    result = await transcribe(
        _config(enabled=True, base_url=LOCAL_STT),
        AUDIO_B64,
        "audio/webm;codecs=opus",
        transport=transport,
    )
    assert result.ok is True
    assert result.text == "hello from the mic"
    assert result.detail == ""
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert str(request.url) == f"{LOCAL_STT}/audio/transcriptions"
    await request.aread()
    body = request.content
    assert b'filename="dictation.webm"' in body
    assert b'name="model"' not in body
    assert request.headers.get("authorization") is None


@pytest.mark.asyncio
async def test_model_and_bearer_are_sent_when_configured() -> None:
    transport = _ScriptedTransport()
    result = await transcribe(
        _config(enabled=True, base_url=LOCAL_STT, model="whisper-1", credential="whisper"),
        AUDIO_B64,
        "audio/wav",
        api_key="sk-test-speech",
        transport=transport,
    )
    assert result.ok is True
    request = transport.requests[0]
    assert request.headers["Authorization"] == "Bearer sk-test-speech"
    await request.aread()
    assert b"whisper-1" in request.content
    assert b'filename="dictation.wav"' in request.content


@pytest.mark.asyncio
async def test_http_failure_never_echoes_the_url() -> None:
    transport = _ScriptedTransport(status=500, body={"error": "nope"})
    result = await transcribe(
        _config(enabled=True, base_url=LOCAL_STT),
        AUDIO_B64,
        "audio/webm",
        transport=transport,
    )
    assert result.ok is False
    assert result.detail == "speech_failed"
    assert LOCAL_STT not in result.detail
    assert "127.0.0.1" not in result.detail
    assert result.text == ""


@pytest.mark.asyncio
async def test_moving_the_base_url_moves_the_destination() -> None:
    transport = _ScriptedTransport()
    moved = "https://somewhere-else.invalid/v1"
    result = await transcribe(
        _config(enabled=True, base_url=moved),
        AUDIO_B64,
        "audio/webm",
        transport=transport,
    )
    assert result.ok is True
    assert str(transport.requests[0].url) == f"{moved}/audio/transcriptions"
