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

from services.api_rate_limiter import limiter
from services.database import get_db
from services.piper_service import PiperService
from services.whisper_service import WhisperService
from utils.config import settings

router = APIRouter()

whisper_service = WhisperService()
piper_service = PiperService()


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
    db: AsyncSession = Depends(get_db)
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

        if settings.speaker_recognition_enabled:
            # Transkription MIT Sprechererkennung
            result = await whisper_service.transcribe_bytes_with_speaker(
                audio_bytes,
                filename=audio.filename,
                db_session=db,
                language=language
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
                language=language
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
async def text_to_speech(request: Request, tts_request: TTSRequest):
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
    """
    Serve cached TTS audio files.

    This endpoint is used by Home Assistant media players to fetch
    TTS audio that was generated by Renfield.

    The audio_id is a unique identifier generated when the audio was cached.
    """
    from services.audio_output_service import get_audio_output_service

    service = get_audio_output_service()
    audio_bytes = service.get_cached_audio(audio_id)

    if not audio_bytes:
        raise HTTPException(status_code=404, detail="Audio not found or expired")

    return StreamingResponse(
        BytesIO(audio_bytes),
        media_type="audio/wav",
        headers={
            "Content-Disposition": f"inline; filename={audio_id}.wav",
            "Cache-Control": "no-cache"
        }
    )


@router.post("/voice-chat")
@limiter.limit(settings.api_rate_limit_voice)
async def voice_chat(
    request: Request,
    audio: UploadFile = File(...),
    language: str | None = Query(None, description="Language code (e.g., 'de', 'en'). Falls back to default."),
    db: AsyncSession = Depends(get_db)
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

        if settings.speaker_recognition_enabled:
            result = await whisper_service.transcribe_bytes_with_speaker(
                audio_bytes,
                filename=audio.filename,
                db_session=db,
                language=language
            )
            user_text = result.get("text", "")
            speaker_name = result.get("speaker_name")
            speaker_alias = result.get("speaker_alias")
            speaker_confidence = result.get("speaker_confidence", 0.0)

            if speaker_name:
                logger.info(f"🎤 Voice-Chat von: {speaker_name} (@{speaker_alias})")
        else:
            user_text = await whisper_service.transcribe_bytes(audio_bytes, audio.filename, language=language)
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
