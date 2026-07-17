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
import os
from typing import Any

import httpx

from utils.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0
# Batch diarization + ASR runs on up to meeting_max_duration_h of audio, so the
# meeting endpoint needs a far larger ceiling than the one-shot STT/TTS default.
# Sized to the cap + a 10-min margin (recomputed per call from settings).
_MEETING_TIMEOUT_MARGIN_S = 600.0


class VoiceServerError(Exception):
    """Voice-server returned a non-2xx response or was unreachable.

    ``status_code`` carries the HTTP status when the server responded (so callers
    can split retryable 5xx / unreachable from terminal 4xx); None when the
    server was unreachable (connect/timeout — always retryable).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _base_url() -> str:
    if not settings.voice_server_url:
        raise VoiceServerError("voice_server_url not configured")
    return settings.voice_server_url.rstrip("/")


def _auth_headers(auth_token: str) -> dict[str, str]:
    """Bearer header — omitted entirely when the token is empty, so a voice-server
    running auth_required=false treats the call as anonymous instead of trying
    (and failing) to validate an empty/foreign token. Callers gate the token on
    ``settings.voice_server_auth_enabled``."""
    return {"Authorization": f"Bearer {auth_token}"} if auth_token else {}


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
    headers = _auth_headers(auth_token)
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


async def stt_opus(
    opus_blob: bytes,
    *,
    language: str | None = None,
    auth_token: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """POST a satellite's raw Opus packet blob to /api/voice/stt-opus.

    The blob is the C1 `[uint16 len][packet]…` framing; the voice-server decodes
    it with opuslib (decode lives on the media layer, design D6). Same return
    shape as stt(): {text, language, speaker_embedding?, audio_duration_s}.
    """
    url = f"{_base_url()}/api/voice/stt-opus"
    headers = _auth_headers(auth_token)
    files = {"audio": ("satellite_audio.opus", opus_blob, "application/octet-stream")}
    data: dict[str, Any] = {}
    if language:
        data["language"] = language

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            resp = await client.post(url, headers=headers, files=files, data=data)
        except httpx.HTTPError as e:
            raise VoiceServerError(f"voice-server STT-opus unreachable: {e}") from e

    if resp.status_code != 200:
        raise VoiceServerError(
            f"voice-server STT-opus returned {resp.status_code}: {resp.text[:300]}"
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
    headers = _auth_headers(auth_token)
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


async def transcribe_meeting(
    audio_path: str,
    *,
    auth_token: str,
    whisper_model: str | None = None,
    num_speakers: int | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """POST a meeting recording (by PATH, streamed) to /transcribe-meeting.

    Long-running batch call (diarization + ASR + per-cluster ECAPA on the whole
    recording). Returns the diarized result, e.g.
    ``{"segments": [{"speaker": "SPEAKER_00", "start_s", "end_s", "text",
    "embedding"?}, ...], "num_speakers": N, "duration_s": ...}``.

    The voice-server endpoint lands in PR 2; this client is the backend seam.
    """
    if timeout_s is None:
        timeout_s = settings.meeting_max_duration_h * 3600 + _MEETING_TIMEOUT_MARGIN_S
    url = f"{_base_url()}/transcribe-meeting"
    headers = _auth_headers(auth_token)
    data: dict[str, Any] = {}
    if whisper_model:
        data["whisper_model"] = whisper_model
    if num_speakers:
        data["num_speakers"] = str(num_speakers)

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            with open(audio_path, "rb") as fh:
                files = {
                    "audio": (os.path.basename(audio_path), fh, "application/octet-stream")
                }
                resp = await client.post(url, headers=headers, files=files, data=data)
        except FileNotFoundError as e:
            # A missing audio file is terminal — retrying won't conjure it back.
            raise VoiceServerError(f"meeting audio missing: {e}", status_code=400) from e
        except httpx.HTTPError as e:
            # Unreachable (connect/timeout) — always retryable (status_code=None).
            raise VoiceServerError(f"voice-server transcribe-meeting unreachable: {e}") from e

    if resp.status_code != 200:
        raise VoiceServerError(
            f"voice-server transcribe-meeting returned {resp.status_code}: {resp.text[:300]}",
            status_code=resp.status_code,
        )
    return resp.json()
