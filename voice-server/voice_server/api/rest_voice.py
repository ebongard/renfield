"""REST endpoints — primitives consumed by backend (B.4.c) and satellites.

**Contract boundary:** voice-server is stateless. These endpoints return
RAW model output (text, language hint, speaker embedding floats). They do
NOT resolve speakers against Postgres or auto-enrol — that is the
backend's job per `services/speaker_service.py`. In B.4.c the backend's
existing `/api/voice/stt` route becomes a thin proxy that calls this
endpoint and adds speaker resolution before responding to the satellite.

Until B.4.c lands, Traefik does NOT route `/api/voice/*` to voice-server
(only `/ws/voice`). Backend keeps owning the satellite-facing path.

  POST /api/voice/stt   multipart audio        → { text, language, speaker_embedding?, audio_duration_s }
  POST /api/voice/tts   { text, language? }    → audio/wav (single-shot, full WAV)
"""

from __future__ import annotations

import io
import logging
import wave

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Header, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from voice_server.auth import AuthError, authenticate
from voice_server.services.audio_oneshot import OneshotDecodeError, decode_audio_to_pcm
from voice_server.services.speaker_service import SpeakerService
from voice_server.services.stt_service import STTService
from voice_server.services.tts_service import TTSService

logger = logging.getLogger(__name__)
router = APIRouter()


async def _require_token(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        # Match WebSocket path: defer to authenticate() so auth_required=False
        # treats missing tokens as anonymous instead of rejecting.
        try:
            return await authenticate("")
        except AuthError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
    token = authorization.split(" ", 1)[1].strip()
    try:
        return await authenticate(token)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e


class STTResponse(BaseModel):
    text: str
    language: str
    audio_duration_s: float
    speaker_embedding: list[float] | None = None


@router.post("/api/voice/stt", response_model=STTResponse)
async def stt_endpoint(
    request: Request,
    audio: UploadFile = File(...),
    language: str | None = Form(default=None),
    _user: dict = Depends(_require_token),
) -> STTResponse:
    """Transcribe a single audio file. One-shot (not streaming)."""
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="empty audio")

    # REST gives us a complete file — let ffmpeg auto-detect the container
    # and codec. The streaming AudioDecoder pins -f to a codec hint
    # because chunks can't be auto-detected, but that hint over-constrains
    # full-file uploads (e.g. browser MediaRecorder webm/opus rejected by
    # `-f webm`). The one-shot decoder also captures stderr so failures
    # produce a real error message instead of silent 0 PCM.
    try:
        pcm = await decode_audio_to_pcm(audio_bytes)
    except OneshotDecodeError as e:
        logger.warning("STT decode failed: %s", e)
        raise HTTPException(status_code=400, detail=f"audio decode failed: {e}") from e

    if pcm.size == 0:
        raise HTTPException(status_code=400, detail="empty PCM after decode")

    stt: STTService = request.app.state.stt
    speaker: SpeakerService = request.app.state.speaker

    text_parts: list[str] = []
    async for seg in stt.transcribe_stream(pcm, language=language):
        text_parts.append(seg.text)
    text = " ".join(t.strip() for t in text_parts if t.strip())
    # Pull the auto-detected language from the side-channel set by
    # transcribe_stream. Falls back to the requested language, then
    # to the service default. Reflects what faster-whisper actually
    # detected on the audio rather than echoing the request.
    detected_language = stt.last_language or language or "de"

    embedding: list[float] | None = None
    try:
        emb = await speaker.embed(pcm)
        embedding = emb.tolist()
    except Exception as e:
        logger.warning("speaker embed failed in REST stt: %s", e)

    return STTResponse(
        text=text,
        language=detected_language,
        audio_duration_s=float(pcm.size) / 16000.0,
        speaker_embedding=embedding,
    )


class TTSRequest(BaseModel):
    text: str
    language: str | None = None
    voice: str | None = None  # reserved; unused — voice picked by language


@router.post("/api/voice/tts")
async def tts_endpoint(
    body: TTSRequest,
    request: Request,
    _user: dict = Depends(_require_token),
) -> Response:
    """Synthesize text to a single WAV file (one-shot — full text in one go)."""
    import uuid

    if not body.text or not body.text.strip():
        raise HTTPException(status_code=400, detail="empty text")

    tts: TTSService = request.app.state.tts

    # Concatenate all sentence WAVs into one. The header per frame from
    # stream_sentences is needed for WS routing; for REST we strip it and
    # concatenate raw WAV bodies.
    wav_chunks: list[bytes] = []
    request_id = uuid.uuid4()
    try:
        async for frame in tts.stream_sentences(body.text, request_id, language=body.language):
            # Strip the 24-byte RFWA header (4+16+4)
            wav_chunks.append(frame[24:])
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    if not wav_chunks:
        raise HTTPException(status_code=500, detail="no audio produced")

    merged = _concat_wavs(wav_chunks)
    return Response(content=merged, media_type="audio/wav")


def _concat_wavs(wav_chunks: list[bytes]) -> bytes:
    """Concatenate WAV byte payloads sharing the same format into one WAV."""
    if len(wav_chunks) == 1:
        return wav_chunks[0]

    # Read first chunk to inherit format
    first = wave.open(io.BytesIO(wav_chunks[0]), "rb")
    nchannels = first.getnchannels()
    sampwidth = first.getsampwidth()
    framerate = first.getframerate()
    frames = [first.readframes(first.getnframes())]
    first.close()

    for chunk in wav_chunks[1:]:
        w = wave.open(io.BytesIO(chunk), "rb")
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (nchannels, sampwidth, framerate):
            raise RuntimeError("WAV format mismatch across sentences")
        frames.append(w.readframes(w.getnframes()))
        w.close()

    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(nchannels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        w.writeframes(b"".join(frames))
    return out.getvalue()
