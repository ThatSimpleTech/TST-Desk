"""Hold-to-talk dictation (TD-4701) — persist, URL join, transcription gate."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from tstd.config import VoiceConfig
from tstd.voice import (
    MAX_AUDIO_BYTES,
    VoiceError,
    load_voice,
    save_voice,
    transcribe_audio,
    transcription_url,
    voice_has_endpoint,
    voice_path,
)


def test_absent_is_off(tmp_path: Path) -> None:
    assert load_voice(tmp_path) is False
    assert not voice_path(tmp_path).exists()


def test_round_trip(tmp_path: Path) -> None:
    save_voice(tmp_path, True)
    assert load_voice(tmp_path) is True
    save_voice(tmp_path, False)
    assert load_voice(tmp_path) is False


def test_lands_in_user_data_not_workspace(tmp_path: Path) -> None:
    data = tmp_path / "data"
    workspace = tmp_path / "ws"
    (workspace / ".tst").mkdir(parents=True)
    workspace_config = workspace / ".tst" / "config.yaml"
    workspace_config.write_text("policy:\n  rules: []\n", encoding="utf-8")
    before = workspace_config.read_text(encoding="utf-8")
    save_voice(data, True)
    assert load_voice(data) is True
    assert workspace_config.read_text(encoding="utf-8") == before
    assert voice_path(data).is_file()


def test_transcription_url_joins_openai_root() -> None:
    assert transcription_url("http://127.0.0.1:8080/v1") == (
        "http://127.0.0.1:8080/v1/audio/transcriptions"
    )
    assert transcription_url("http://127.0.0.1:8080/v1/") == (
        "http://127.0.0.1:8080/v1/audio/transcriptions"
    )


def test_empty_base_url_is_not_an_endpoint() -> None:
    assert voice_has_endpoint(VoiceConfig()) is False
    assert voice_has_endpoint(VoiceConfig(base_url="http://127.0.0.1:8080/v1")) is True


@pytest.mark.asyncio
async def test_transcribe_posts_to_configured_host() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers.get("authorization") is None
        return httpx.Response(200, json={"text": "  hello world  "})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    text = await transcribe_audio(
        b"RIFF",
        "audio/webm",
        VoiceConfig(base_url="http://127.0.0.1:8080/v1"),
        client=client,
    )
    await client.aclose()
    assert text == "hello world"
    assert seen == ["http://127.0.0.1:8080/v1/audio/transcriptions"]


@pytest.mark.asyncio
async def test_transcribe_refuses_empty_endpoint() -> None:
    with pytest.raises(VoiceError) as excinfo:
        await transcribe_audio(b"x", "audio/webm", VoiceConfig())
    assert excinfo.value.code == "no_speech_endpoint"


@pytest.mark.asyncio
async def test_transcribe_refuses_oversize() -> None:
    with pytest.raises(VoiceError) as excinfo:
        await transcribe_audio(b"x" * (MAX_AUDIO_BYTES + 1), "audio/webm", VoiceConfig())
    assert excinfo.value.code == "attachment_too_large"


@pytest.mark.asyncio
async def test_transcribe_refuses_remote_without_credential() -> None:
    with pytest.raises(VoiceError) as excinfo:
        await transcribe_audio(
            b"x",
            "audio/webm",
            VoiceConfig(base_url="https://speech.invalid/v1"),
        )
    assert excinfo.value.code == "auth_failed"


def test_audio_b64_round_trip_size() -> None:
    raw = b"\x00\x01\x02"
    encoded = base64.b64encode(raw).decode("ascii")
    assert base64.b64decode(encoded, validate=True) == raw
