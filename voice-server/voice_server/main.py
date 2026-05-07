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

    # Lazy-load heavy services to keep cold-start visible in logs.
    from voice_server.services.speaker_service import SpeakerService
    from voice_server.services.stt_service import STTService
    from voice_server.services.tts_service import TTSService

    app.state.stt = STTService()
    app.state.tts = TTSService()
    app.state.speaker = SpeakerService()
    app.state.xtts = None  # populated below if XTTS is enabled

    # B.5 spike — XTTS-v2 service is built only when explicitly enabled
    # (the spike image sets XTTS_ENABLED=true). Production v0.1.5 doesn't
    # ship coqui-tts so the import path here would error out — gating
    # on the flag keeps the same image runnable in both modes.
    if settings.xtts_enabled:
        from voice_server.services.xtts_service import XTTSService

        app.state.xtts = XTTSService()
        logger.info("xtts service registered (lazy-load on first request)")

    await app.state.stt.warmup()
    await app.state.speaker.warmup()
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
    return {
        "status": "ok" if (stt_ready and spk_ready) else "warming",
        "version": __version__,
        "stt_ready": stt_ready,
        "speaker_ready": spk_ready,
    }


# Routers wired in B.1.5 (WS) and B.1.6 (REST).
from voice_server.api.ws_voice import router as ws_voice_router  # noqa: E402
from voice_server.api.rest_voice import router as rest_voice_router  # noqa: E402

app.include_router(ws_voice_router)
app.include_router(rest_voice_router)
