"""voice-server FastAPI app.

Endpoints:
  GET  /health
  WS   /ws/voice               — streaming voice protocol
  POST /api/voice/stt          — REST STT for satellites (B.1.6)
  POST /api/voice/tts          — REST TTS for satellites (B.1.6)
"""

from __future__ import annotations

import contextlib
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from voice_server import __version__
from voice_server.config import settings

logger = logging.getLogger("voice_server")


class _AnonServer(uvicorn.Server):
    """In-process second listener that does NOT own process signals.

    uvicorn >= 0.30 registers SIGTERM/SIGINT handlers unconditionally inside
    serve() via the capture_signals() context manager (the old
    Server.install_signal_handlers hook no longer exists). Without this
    override the anon listener — started AFTER the primary — would overwrite
    the primary's handlers, so a K8s SIGTERM would flip the ANON server's
    should_exit, the primary would never drain, and kubelet would SIGKILL
    both at the grace deadline (PR #987 review finding 1). The primary
    server owns signals; this one is stopped explicitly in the lifespan
    finally.
    """

    @contextlib.contextmanager
    def capture_signals(self):
        yield


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

    # Registry mode with an anonymous client row (D16: the household has no
    # user logins) serves the SAME app on a second port. The deployment
    # fences that port with a NetworkPolicy + a dedicated ClusterIP Service;
    # auth binds anonymous rows to it via the ASGI server scope, so the
    # primary (ingress-reachable) port never honors them. lifespan="off" —
    # app.state is already initialized by THIS lifespan; a second run would
    # reload the models.
    anon_server = None
    anon_task = None
    if settings.auth_mode == "registry" and any(
        c.anonymous for c in settings.auth_clients.values()
    ):
        import asyncio
        import time as _time

        anon_config = uvicorn.Config(
            app,
            host=settings.host,
            port=settings.anon_port,
            log_level=settings.log_level.lower(),
            lifespan="off",
        )
        anon_server = _AnonServer(anon_config)

        async def _serve_anon() -> None:
            # uvicorn calls sys.exit() on a bind failure; keep that (and any
            # other BaseException) inside this task so it can't tear through
            # the shared event loop, and let the started-wait below turn it
            # into a loud lifespan failure.
            try:
                await anon_server.serve()
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001 — includes SystemExit
                logger.exception("anonymous-client listener crashed")

        anon_task = asyncio.get_running_loop().create_task(_serve_anon())

        # Fail LOUD if the anon port can't bind (same philosophy as the
        # opuslib boot check above): a dead household listener behind a green
        # /health is exactly the silent-failure class this project is
        # digging out of. Lifespan failure → non-ready pod → visible.
        deadline = _time.monotonic() + 10.0
        while not anon_server.started:
            if anon_task.done() or _time.monotonic() > deadline:
                raise RuntimeError(
                    f"anonymous-client listener failed to start on "
                    f":{settings.anon_port}"
                )
            await asyncio.sleep(0.05)
        logger.info("anonymous-client listener on :%d", settings.anon_port)

    logger.info("voice-server ready")
    try:
        yield
    finally:
        if anon_server is not None:
            anon_server.should_exit = True
            if anon_task is not None:
                try:
                    await anon_task
                except Exception:  # noqa: BLE001 — shutdown path, log only
                    logger.exception("anon listener shutdown error")
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
