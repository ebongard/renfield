"""
Voice API Routes (Speech-to-Text & Text-to-Speech)
"""
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from loguru import logger
from io import BytesIO

from services.whisper_service import WhisperService
from services.piper_service import PiperService

router = APIRouter()

whisper_service = WhisperService()
piper_service = PiperService()

class TTSRequest(BaseModel):
    text: str
    voice: str = "de_DE-thorsten-high"

@router.post("/stt")
async def speech_to_text(
    audio: UploadFile = File(...)
):
    """Speech-to-Text: Audio zu Text konvertieren"""
    try:
        logger.info(f"🎤 STT-Anfrage erhalten: {audio.filename}, Content-Type: {audio.content_type}")
        
        # Audio-Bytes lesen
        audio_bytes = await audio.read()
        logger.info(f"📊 Audio-Größe: {len(audio_bytes)} bytes")
        
        # Transkribieren
        logger.info("🔄 Starte Transkription...")
        text = await whisper_service.transcribe_bytes(
            audio_bytes,
            filename=audio.filename
        )
        
        if not text:
            logger.error("❌ Transkription ergab leeren Text")
            raise HTTPException(status_code=400, detail="Transkription fehlgeschlagen")
        
        logger.info(f"✅ Transkription erfolgreich: '{text[:100]}'")
        
        return {
            "text": text,
            "language": "de"
        }
    except Exception as e:
        logger.error(f"❌ STT Fehler: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/tts")
async def text_to_speech(request: TTSRequest):
    """Text-to-Speech: Text zu Audio konvertieren"""
    try:
        # TTS generieren
        audio_bytes = await piper_service.synthesize_to_bytes(request.text)
        
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

@router.post("/voice-chat")
async def voice_chat(
    audio: UploadFile = File(...)
):
    """
    Kompletter Voice-Chat Flow:
    1. Audio zu Text (STT)
    2. Text an Ollama
    3. Antwort zu Audio (TTS)
    """
    try:
        # 1. Speech-to-Text
        audio_bytes = await audio.read()
        user_text = await whisper_service.transcribe_bytes(audio_bytes, audio.filename)
        
        if not user_text:
            raise HTTPException(status_code=400, detail="Konnte Audio nicht verstehen")
        
        # 2. Chat mit Ollama
        from main import app
        from services.ollama_service import OllamaService
        
        ollama: OllamaService = app.state.ollama
        response_text = await ollama.chat(user_text)
        
        # 3. Text-to-Speech
        response_audio = await piper_service.synthesize_to_bytes(response_text)
        
        return {
            "user_text": user_text,
            "assistant_text": response_text,
            "audio": response_audio.hex() if response_audio else None
        }
    except Exception as e:
        logger.error(f"❌ Voice Chat Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))
