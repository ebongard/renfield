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

    # B.5 spike fields — accepted only when settings.xtts_enabled. Production
    # callers (default `engine="piper"`) see no behaviour change.
    engine: str | None = None  # "piper" | "xtts-default" | "xtts-clone"
    voice_ref: str | None = None  # absolute path to clone reference; engine=xtts-clone only


@router.post("/api/voice/tts")
async def tts_endpoint(
    body: TTSRequest,
    request: Request,
    _user: dict = Depends(_require_token),
) -> Response:
    """Synthesize text to a single WAV file (one-shot — full text in one go).

    Engines:
      - `piper` (default) — production path; uses TTSService.stream_sentences.
      - `xtts-default` — B.5 spike; XTTS-v2 with built-in speaker.
      - `xtts-clone` — B.5 spike; XTTS-v2 with speaker_wav cloning.
    """
    import uuid

    if not body.text or not body.text.strip():
        raise HTTPException(status_code=400, detail="empty text")

    engine = (body.engine or "piper").lower()

    if engine == "piper":
        return await _tts_piper(body, request)
    if engine in ("xtts-default", "xtts-clone"):
        return await _tts_xtts(body, request, engine)
    raise HTTPException(status_code=400, detail=f"unknown engine: {engine}")


async def _tts_piper(body: TTSRequest, request: Request) -> Response:
    import time
    import uuid

    tts: TTSService = request.app.state.tts

    # Concatenate all sentence WAVs into one. The header per frame from
    # stream_sentences is needed for WS routing; for REST we strip it and
    # concatenate raw WAV bodies.
    #
    # Per-chunk timing is captured in X-Synth-Chunk-Times-Ms so the B.5
    # benchmark sees the same shape across all engines (Piper and XTTS).
    # `stream_sentences` yields one frame per sentence; we time the gap
    # between consecutive yields and record cumulative wall-clock per
    # chunk. First-chunk time = first-sentence TTFB at the synth layer.
    wav_chunks: list[bytes] = []
    chunk_times_ms: list[float] = []
    request_id = uuid.uuid4()
    last_t = time.monotonic()
    try:
        async for frame in tts.stream_sentences(body.text, request_id, language=body.language):
            now = time.monotonic()
            chunk_times_ms.append((now - last_t) * 1000.0)
            last_t = now
            # Strip the 24-byte RFWA header (4+16+4)
            wav_chunks.append(frame[24:])
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    if not wav_chunks:
        raise HTTPException(status_code=500, detail="no audio produced")

    merged = _concat_wavs(wav_chunks)
    headers = {"X-Synth-Chunk-Times-Ms": ",".join(f"{t:.2f}" for t in chunk_times_ms)}
    return Response(content=merged, media_type="audio/wav", headers=headers)


async def _tts_xtts(body: TTSRequest, request: Request, engine: str) -> Response:
    """B.5 spike — XTTS engine path. Returns 503 if xtts_enabled=False."""
    from pathlib import Path

    from voice_server.config import settings as _settings

    xtts = getattr(request.app.state, "xtts", None)
    if xtts is None:
        raise HTTPException(
            status_code=503,
            detail="xtts not available (set XTTS_ENABLED=true in spike deployment)",
        )

    # voice_ref resolution. xtts-default ignores voice_ref entirely; xtts-clone
    # takes the body's voice_ref if provided, else the default clone-ref path
    # from settings (Step 3 deliverable: thorsten_ref.wav).
    voice_ref: Path | None = None
    if engine == "xtts-clone":
        ref_path = body.voice_ref or _settings.xtts_clone_voice_ref
        if ref_path is None:
            raise HTTPException(
                status_code=400,
                detail="xtts-clone requires voice_ref (request body or settings)",
            )
        voice_ref = Path(ref_path) if isinstance(ref_path, str) else ref_path
        if not voice_ref.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"voice_ref not found: {voice_ref}",
            )

    try:
        wav = await xtts.synth_one(
            text=body.text,
            voice_ref=voice_ref,
            language=body.language or "de",
        )
    except Exception as e:
        logger.exception("xtts synth failed")
        raise HTTPException(status_code=500, detail=f"xtts synth failed: {e}") from e

    if not wav:
        raise HTTPException(status_code=500, detail="xtts produced no audio")

    # Expose per-chunk timing as a header for the benchmark to read first-
    # chunk TTFB and total time without parsing response timing. Comma-
    # separated milliseconds, in order. The benchmark in Step 5 reads this.
    chunk_times = ",".join(f"{t:.2f}" for t in xtts.last_chunk_times_ms)
    headers = {"X-Synth-Chunk-Times-Ms": chunk_times}
    return Response(content=wav, media_type="audio/wav", headers=headers)


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
