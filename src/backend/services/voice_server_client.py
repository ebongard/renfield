"""HTTP client for the voice-server (B.4 backend integration).

The voice-server (k8s-gpu-3) exposes:
  POST /api/voice/stt   multipart audio        → { text, language, speaker_embedding?, audio_duration_s }
  POST /api/voice/tts   { text, language? }    → audio/wav

This module wraps both endpoints so backend services don't repeat
HTTP plumbing. Activated when settings.voice_server_url is set;
backend's existing whisper_service / piper_service stay around as
the in-process fallback for dev environments.

Auth: voice-server validates the same JWT the backend issued for the
caller. We forward the caller's bearer token via Authorization
header. For the satellite-orchestrator path (`voice-chat` route),
we mint a service-account token via existing auth_service helpers
because the satellite uses cookie/session auth that isn't a JWT.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from utils.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0


class VoiceServerError(Exception):
    """Voice-server returned a non-2xx response or was unreachable."""


def _base_url() -> str:
    if not settings.voice_server_url:
        raise VoiceServerError("voice_server_url not configured")
    return settings.voice_server_url.rstrip("/")


async def stt(
    audio_bytes: bytes,
    *,
    filename: str = "audio.wav",
    content_type: str = "audio/wav",
    language: str | None = None,
    auth_token: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """POST audio to voice-server /api/voice/stt.

    Returns {text, language, speaker_embedding?, audio_duration_s}.
    """
    url = f"{_base_url()}/api/voice/stt"
    headers = {"Authorization": f"Bearer {auth_token}"}
    files = {"audio": (filename, audio_bytes, content_type)}
    data: dict[str, Any] = {}
    if language:
        data["language"] = language

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            resp = await client.post(url, headers=headers, files=files, data=data)
        except httpx.HTTPError as e:
            raise VoiceServerError(f"voice-server STT unreachable: {e}") from e

    if resp.status_code != 200:
        raise VoiceServerError(
            f"voice-server STT returned {resp.status_code}: {resp.text[:300]}"
        )

    return resp.json()


async def tts(
    text: str,
    *,
    language: str | None = None,
    auth_token: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> bytes:
    """POST text to voice-server /api/voice/tts. Returns full WAV bytes."""
    url = f"{_base_url()}/api/voice/tts"
    headers = {"Authorization": f"Bearer {auth_token}"}
    payload: dict[str, Any] = {"text": text}
    if language:
        payload["language"] = language

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            resp = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as e:
            raise VoiceServerError(f"voice-server TTS unreachable: {e}") from e

    if resp.status_code != 200:
        raise VoiceServerError(
            f"voice-server TTS returned {resp.status_code}: {resp.text[:300]}"
        )
    return resp.content
