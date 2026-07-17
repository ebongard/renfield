"""voice-server FastAPI app.

Endpoints:
  GET  /health
  WS   /ws/voice               — streaming voice protocol
  POST /api/voice/stt          — REST STT for satellites (B.1.6)
  POST /api/voice/tts          — REST TTS for satellites (B.1.6)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from voice_server import __version__
from voice_server.config import settings

logger = logging.getLogger("voice_server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logger.info("voice-server %s starting", __version__)
    logger.info(
        "config: stt=%s/%s tts=%s spk=%s auth=%s",
        settings.whisper_model,
        settings.whisper_compute_type,
        settings.piper_voices_dir,
        settings.speaker_model_path,
        settings.auth_mode,
    )

    # Fail LOUD at boot if the image can't decode satellite opus — otherwise a
    # skewed deploy (backend negotiates opus, this image lacks libopus) would
    # only surface as a 503 on the first satellite utterance. /stt-opus still
    # 503s per-request; this just makes the misconfiguration obvious at startup.
    from voice_server.services.opus_decode import OPUSLIB_AVAILABLE

    if not OPUSLIB_AVAILABLE:
        logger.warning(
            "opuslib/libopus NOT available — /api/voice/stt-opus will 503. "
            "If any satellite negotiates opus (SATELLITE_OPUS_ENABLED), rebuild "
            "this image with libopus0 + opuslib."
        )

    # Lazy-load heavy services to keep cold-start visible in logs.
    from voice_server.services.speaker_service import SpeakerService
    from voice_server.services.stt_service import STTService
    from voice_server.services.tts_service import TTSService

    app.state.stt = STTService()
    app.state.tts = TTSService()
    app.state.speaker = SpeakerService()

    await app.state.stt.warmup()
    await app.state.speaker.warmup()

    # Meeting diarization (§2): only loads pyannote (~2 GB VRAM) when enabled, so
    # a non-meeting deployment is unaffected. warmup() no-ops when off.
    from voice_server.services.meeting_service import MeetingDiarizationService

    app.state.meeting = MeetingDiarizationService()
    await app.state.meeting.warmup()

    logger.info("voice-server ready")
    try:
        yield
    finally:
        logger.info("voice-server shutting down")


app = FastAPI(
    title="renfield-voice-server",
    version=__version__,
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, object]:
    stt_ready = getattr(app.state, "stt", None) is not None and app.state.stt.ready
    spk_ready = getattr(app.state, "speaker", None) is not None and app.state.speaker.ready
    meeting = getattr(app.state, "meeting", None)
    return {
        "status": "ok" if (stt_ready and spk_ready) else "warming",
        "version": __version__,
        "stt_ready": stt_ready,
        "speaker_ready": spk_ready,
        # informational — meeting diarization is off unless MEETING_ENABLED
        "meeting_ready": meeting is not None and meeting.ready,
    }


# Routers wired in B.1.5 (WS) and B.1.6 (REST).
from voice_server.api.ws_voice import router as ws_voice_router  # noqa: E402
from voice_server.api.rest_voice import router as rest_voice_router  # noqa: E402

app.include_router(ws_voice_router)
app.include_router(rest_voice_router)
