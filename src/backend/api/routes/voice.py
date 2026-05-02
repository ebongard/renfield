"""
Voice API Routes (Speech-to-Text & Text-to-Speech)

Multi-language support:
- STT: Pass ?language=en to transcribe in a specific language
- TTS: Pass {"language": "en"} to synthesize in a specific language
"""
from io import BytesIO

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.websocket.shared import get_whisper_service
from services.api_rate_limiter import limiter
from services.auth_service import get_current_user
from services.database import AsyncSessionLocal, get_db
from services.piper_service import get_piper_service
from utils.config import settings

router = APIRouter()

# Use the shared singletons. The previous `WhisperService()` + `PiperService()`
# instantiations at module level loaded the model a SECOND time when the
# WebSocket path also lazily created its own — verified two competing
# WhisperService objects in flight. `get_whisper_service` (lives in
# api/websocket/shared.py) and `get_piper_service` (lives in piper_service.py)
# are the canonical accessors.
whisper_service = get_whisper_service()
piper_service = get_piper_service()


class TTSRequest(BaseModel):
    text: str
    voice: str | None = None  # Deprecated: use language instead
    language: str | None = None  # Language code (e.g., 'de', 'en')

@router.post("/stt")
@limiter.limit(settings.api_rate_limit_voice)
async def speech_to_text(
    request: Request,
    audio: UploadFile = File(...),
    language: str | None = Query(None, description="Language code (e.g., 'de', 'en'). Falls back to default."),
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user)
):
    """
    Speech-to-Text: Audio zu Text konvertieren mit optionaler Sprechererkennung.

    Multi-language support: Pass ?language=en to transcribe in English, etc.
    """
    try:
        # Validate language if provided
        if language and language.lower() not in settings.supported_languages_list:
            logger.warning(f"⚠️ Unsupported language '{language}', falling back to default")
            language = None

        effective_language = language or settings.default_language
        logger.info(f"🎤 STT-Anfrage erhalten: {audio.filename}, Content-Type: {audio.content_type}, Language: {effective_language}")

        # Audio-Bytes lesen
        audio_bytes = await audio.read()
        logger.info(f"📊 Audio-Größe: {len(audio_bytes)} bytes")

        # Transkribieren mit Sprechererkennung (wenn aktiviert)
        logger.info("🔄 Starte Transkription...")

        # B-3: per-request STT bias prompt. The /stt endpoint has the
        # authenticated user (via Depends) but no room_context — we pass
        # user_id only and let the platform-default builder compose a
        # household-scoped prompt without room-specific bias.
        # NOTE: open a SEPARATE session for the prompt build so the
        # transaction lifecycle is independent of `db`. transcribe_with_speaker
        # commits mid-flight on auto-enroll; rolling that into the same
        # session as the prompt SELECTs would silently put any future DB work
        # in the request handler into a post-commit fresh transaction.
        from services.whisper_prompt_builder import get_whisper_prompt_builder

        prompt_builder = get_whisper_prompt_builder()
        async with AsyncSessionLocal() as prompt_db:
            initial_prompt = await prompt_builder.build(
                user_id=getattr(_user, "id", None),
                room_id=None,
                language=effective_language,
                db_session=prompt_db,
            )

        if settings.speaker_recognition_enabled:
            # Transkription MIT Sprechererkennung
            result = await whisper_service.transcribe_bytes_with_speaker(
                audio_bytes,
                filename=audio.filename,
                db_session=db,
                language=language,
                initial_prompt=initial_prompt,
            )
            text = result.get("text", "")
            speaker_id = result.get("speaker_id")
            speaker_name = result.get("speaker_name")
            speaker_alias = result.get("speaker_alias")
            speaker_confidence = result.get("speaker_confidence", 0.0)

            if speaker_name:
                logger.info(f"🎤 Sprecher erkannt: {speaker_name} (@{speaker_alias}) - Konfidenz: {speaker_confidence:.2f}")
            else:
                logger.info("🎤 Sprecher nicht erkannt (unbekannt oder unter Threshold)")
        else:
            # Transkription OHNE Sprechererkennung
            text = await whisper_service.transcribe_bytes(
                audio_bytes,
                filename=audio.filename,
                language=language,
                initial_prompt=initial_prompt,
            )
            speaker_id = None
            speaker_name = None
            speaker_alias = None
            speaker_confidence = 0.0

        if not text:
            logger.error("❌ Transkription ergab leeren Text")
            raise HTTPException(status_code=400, detail="Transkription fehlgeschlagen")

        logger.info(f"✅ Transkription erfolgreich: '{text[:100]}'")

        return {
            "text": text,
            "language": effective_language,
            "speaker_id": speaker_id,
            "speaker_name": speaker_name,
            "speaker_alias": speaker_alias,
            "speaker_confidence": speaker_confidence
        }
    except Exception as e:
        logger.error(f"❌ STT Fehler: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/tts")
@limiter.limit(settings.api_rate_limit_voice)
async def text_to_speech(request: Request, tts_request: TTSRequest, _user=Depends(get_current_user)):
    """
    Text-to-Speech: Text zu Audio konvertieren.

    Multi-language support: Pass {"language": "en"} to synthesize in English, etc.
    """
    try:
        # Validate language if provided
        language = tts_request.language
        if language and language.lower() not in settings.supported_languages_list:
            logger.warning(f"⚠️ Unsupported language '{language}', falling back to default")
            language = None

        effective_language = language or settings.default_language
        logger.info(f"🔊 TTS request: {len(tts_request.text)} chars, language: {effective_language}")

        # TTS generieren with language support
        audio_bytes = await piper_service.synthesize_to_bytes(tts_request.text, language=language)

        if not audio_bytes:
            raise HTTPException(status_code=400, detail="TTS-Generierung fehlgeschlagen")

        # Als WAV-Stream zurückgeben
        return StreamingResponse(
            BytesIO(audio_bytes),
            media_type="audio/wav",
            headers={
                "Content-Disposition": "attachment; filename=speech.wav"
            }
        )
    except Exception as e:
        logger.error(f"❌ TTS Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/tts-cache/{audio_id}")
async def get_tts_cache(audio_id: str):
    """Serve cached TTS audio files.

    Used by HA media players that fetch pre-generated TTS via HTTP.
    The actual cache lookup runs through the `fetch_tts_audio_cache`
    hook so ha_glue owns the cache storage and platform has no
    direct coupling to `audio_output_service`. On pro deploys (no
    ha_glue handler), the endpoint returns 404 — which is fine
    because pro deploys don't have HA media players calling it.
    """
    from utils.hooks import run_hooks

    audio_bytes = None
    results = await run_hooks("fetch_tts_audio_cache", audio_id=audio_id)
    for result in results:
        if isinstance(result, (bytes, bytearray)):
            audio_bytes = bytes(result)
            break
        if result is not None:
            logger.warning(
                f"⚠️  fetch_tts_audio_cache handler returned unexpected "
                f"shape (type={type(result).__name__}); ignoring"
            )

    if not audio_bytes:
        raise HTTPException(status_code=404, detail="Audio not found or expired")

    return StreamingResponse(
        BytesIO(audio_bytes),
        media_type="audio/wav",
        headers={
            "Content-Disposition": f"inline; filename={audio_id}.wav",
            "Cache-Control": "no-cache",
        },
    )


@router.post("/voice-chat")
@limiter.limit(settings.api_rate_limit_voice)
async def voice_chat(
    request: Request,
    audio: UploadFile = File(...),
    language: str | None = Query(None, description="Language code (e.g., 'de', 'en'). Falls back to default."),
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user)
):
    """
    Kompletter Voice-Chat Flow:
    1. Audio zu Text (STT) mit Sprechererkennung
    2. Text an Ollama
    3. Antwort zu Audio (TTS)

    Multi-language support: Pass ?language=en to use English for both STT and TTS.
    """
    try:
        # Validate language if provided
        if language and language.lower() not in settings.supported_languages_list:
            logger.warning(f"⚠️ Unsupported language '{language}', falling back to default")
            language = None

        effective_language = language or settings.default_language
        logger.info(f"🎤 Voice-Chat request, language: {effective_language}")

        # 1. Speech-to-Text mit Sprechererkennung
        audio_bytes = await audio.read()

        # B-3: per-request STT bias from authenticated user. See `/stt` above
        # for the rationale on the separate session.
        from services.whisper_prompt_builder import get_whisper_prompt_builder

        prompt_builder = get_whisper_prompt_builder()
        async with AsyncSessionLocal() as prompt_db:
            initial_prompt = await prompt_builder.build(
                user_id=getattr(_user, "id", None),
                room_id=None,
                language=effective_language,
                db_session=prompt_db,
            )

        if settings.speaker_recognition_enabled:
            result = await whisper_service.transcribe_bytes_with_speaker(
                audio_bytes,
                filename=audio.filename,
                db_session=db,
                language=language,
                initial_prompt=initial_prompt,
            )
            user_text = result.get("text", "")
            speaker_name = result.get("speaker_name")
            speaker_alias = result.get("speaker_alias")
            speaker_confidence = result.get("speaker_confidence", 0.0)

            if speaker_name:
                logger.info(f"🎤 Voice-Chat von: {speaker_name} (@{speaker_alias})")
        else:
            user_text = await whisper_service.transcribe_bytes(
                audio_bytes,
                audio.filename,
                language=language,
                initial_prompt=initial_prompt,
            )
            speaker_name = None
            speaker_alias = None
            speaker_confidence = 0.0

        if not user_text:
            raise HTTPException(status_code=400, detail="Konnte Audio nicht verstehen")

        # 2. Chat mit Ollama
        from main import app
        from services.ollama_service import OllamaService

        ollama: OllamaService = app.state.ollama
        response_text = await ollama.chat(user_text)

        # 3. Text-to-Speech (using same language)
        response_audio = await piper_service.synthesize_to_bytes(response_text, language=language)

        return {
            "user_text": user_text,
            "assistant_text": response_text,
            "audio": response_audio.hex() if response_audio else None,
            "language": effective_language,
            "speaker_name": speaker_name,
            "speaker_alias": speaker_alias,
            "speaker_confidence": speaker_confidence
        }
    except Exception as e:
        logger.error(f"❌ Voice Chat Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))
