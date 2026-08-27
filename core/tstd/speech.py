"""Speech-to-text via a user-configured transcriptions endpoint (TD-4701).

Hold-to-talk records in the window; this module POSTs
``{base_url}/audio/transcriptions``. The host comes from ``speech.base_url``
in the user config — never a Python literal, never a cloud default. Off
by default. Empty ``base_url`` is unconfigured. Failures never log the URL.
"""

from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from .keychain import KeychainError, get_api_key
from .logging import get_logger
from .protocol import Transcript

if TYPE_CHECKING:
    from .config import ModelConfig

log = get_logger("tstd.speech")

# Caps the recording the window may send. ~30s of opus webm stays under this.
MAX_AUDIO_BYTES = 2 * 1024 * 1024

_MIME_EXT: dict[str, str] = {
    "audio/webm": "webm",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "video/webm": "webm",
}


def _fail(detail: str) -> Transcript:
    return Transcript(ok=False, text="", detail=detail)


def _normalize_mime(mime: str) -> tuple[str, str] | None:
    media = mime.strip().split(";", 1)[0].strip().casefold()
    ext = _MIME_EXT.get(media)
    if ext is None:
        return None
    return media, ext


def _transcription_url(base_url: str) -> str | None:
    base = base_url.strip().rstrip("/")
    if not base:
        return None
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return f"{base}/audio/transcriptions"


async def transcribe(
    config: ModelConfig,
    audio_b64: str,
    mime: str,
    *,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Transcript:
    """Decode *audio_b64* and POST it to the configured transcriptions URL.

    Returns a connection-scoped ``transcript``. ``detail`` is a short code
    on failure and never contains the destination URL.
    """
    speech = config.speech
    if not speech.enabled:
        return _fail("speech_disabled")
    url = _transcription_url(speech.base_url)
    if url is None:
        return _fail("speech_unconfigured")
    normalized = _normalize_mime(mime)
    if normalized is None:
        return _fail("speech_bad_audio")
    media, ext = normalized
    try:
        audio = base64.b64decode(audio_b64, validate=True)
    except (binascii.Error, ValueError):
        return _fail("speech_bad_audio")
    if not audio:
        return _fail("speech_bad_audio")
    if len(audio) > MAX_AUDIO_BYTES:
        return _fail("speech_too_large")

    token = api_key
    if speech.credential and token is None:
        try:
            token = await get_api_key(speech.credential)
        except (KeychainError, FileNotFoundError):
            return _fail("speech_auth")

    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    form: dict[str, str] = {"response_format": "json"}
    if speech.model:
        form["model"] = speech.model
    files = {"file": (f"dictation.{ext}", audio, media)}
    try:
        async with httpx.AsyncClient(timeout=speech.timeout_seconds, transport=transport) as client:
            response = await client.post(url, data=form, files=files, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        # httpx error text often includes the request URL — do not log it.
        log.warning("speech transcribe failed")
        return _fail("speech_failed")
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        return _fail("speech_failed")
    return Transcript(ok=True, text=text.strip(), detail="")
