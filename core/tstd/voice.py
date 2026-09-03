"""Hold-to-talk dictation (TD-4701).

The mic is never always-on. Settings default off. Transcription is either
OS dictation in the composer (no network from this process) or a POST to
``voice.base_url`` from config — the host is never a Python literal.

Audio bytes are not logged.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import yaml

from .config import VoiceConfig, is_loopback_url
from .keychain import KeychainError, get_api_key
from .logging import get_logger

log = get_logger("tstd.voice")

MAX_AUDIO_BYTES = 384_000
_DEFAULT_TIMEOUT = 30.0


class VoiceError(Exception):
    """A refused or failed transcription, with a wire code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def voice_path(data_dir: str | Path) -> Path:
    """Path of the user-data file that holds the dictation bit."""
    return Path(data_dir) / "voice.yaml"


def load_voice(data_dir: str | Path) -> bool:
    """Load dictation. Absent, empty, or unreadable is off."""
    path = voice_path(data_dir)
    if not path.is_file():
        return False
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(raw, dict):
        return False
    return raw.get("enabled") is True


def save_voice(data_dir: str | Path, enabled: bool) -> None:
    """Persist dictation atomically in the user data dir."""
    path = voice_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".voice.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump({"enabled": enabled}, handle, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def transcription_url(base_url: str) -> str:
    """``{base_url}/audio/transcriptions`` — OpenAI-compatible root."""
    root = base_url.strip().rstrip("/") + "/"
    return urljoin(root, "audio/transcriptions")


def voice_has_endpoint(config: VoiceConfig) -> bool:
    return bool(config.base_url.strip())


async def transcribe_audio(
    audio: bytes,
    mime: str,
    config: VoiceConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    """POST the clip to the configured transcription endpoint.

    Raises ``VoiceError`` rather than returning empty text on failure.
    """
    if len(audio) > MAX_AUDIO_BYTES:
        raise VoiceError(
            "attachment_too_large",
            f"That clip is {len(audio)} bytes; the limit is {MAX_AUDIO_BYTES}. "
            "Hold a shorter phrase, or use OS dictation.",
        )
    if not audio:
        raise VoiceError("bad_request", "No audio arrived.")
    base = config.base_url.strip()
    if not base:
        raise VoiceError(
            "no_speech_endpoint",
            "No transcription endpoint is configured. Set voice.base_url in "
            "config.yaml, or use OS dictation.",
        )
    url = transcription_url(base)
    headers: dict[str, str] = {}
    credential = config.credential.strip()
    if credential:
        try:
            key = await get_api_key(credential)
        except KeychainError as exc:
            raise VoiceError("keychain_error", str(exc)) from exc
        headers["Authorization"] = f"Bearer {key}"
    elif not is_loopback_url(base):
        raise VoiceError(
            "auth_failed",
            "voice.base_url is off-box and has no credential. Name a key under "
            "voice.credential, or point base_url at a loopback server.",
        )
    timeout = config.timeout_seconds or _DEFAULT_TIMEOUT
    safe_mime = mime.strip() or "application/octet-stream"
    files = {"file": ("speech.webm", audio, safe_mime)}
    owned = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await http.post(url, headers=headers, files=files, timeout=timeout)
    except httpx.HTTPError as exc:
        raise VoiceError("provider_error", "The transcription endpoint did not answer.") from exc
    finally:
        if owned:
            await http.aclose()
    if response.status_code >= 400:
        raise VoiceError(
            "provider_error",
            f"Transcription failed ({response.status_code}).",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise VoiceError("provider_error", "The transcription endpoint returned non-JSON.") from exc
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise VoiceError("provider_error", "The transcription endpoint returned no text.")
    host = urlsplit(url).hostname or ""
    log.info(
        "transcribed",
        extra={"extra_fields": {"host": host, "bytes": len(audio), "chars": len(text)}},
    )
    return text.strip()
