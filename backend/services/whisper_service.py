"""
Whisper Service - Speech to Text
"""
import whisper
from pathlib import Path
from loguru import logger
from utils.config import settings
import tempfile

class WhisperService:
    """Service für Speech-to-Text mit Whisper"""
    
    def __init__(self):
        self.model_size = settings.whisper_model
        self.model = None
        self.language = "de"
    
    def load_model(self):
        """Modell laden"""
        if self.model is None:
            try:
                logger.info(f"📥 Lade Whisper Modell '{self.model_size}'...")
                
                # OpenAI Whisper verwenden
                self.model = whisper.load_model(self.model_size)
                
                logger.info(f"✅ Whisper Modell geladen")
            except Exception as e:
                logger.error(f"❌ Fehler beim Laden des Whisper Modells: {e}")
                raise
    
    async def transcribe_file(self, audio_path: str) -> str:
        """Audio-Datei transkribieren"""
        if self.model is None:
            self.load_model()
        
        try:
            # Transkribieren mit OpenAI Whisper
            result = self.model.transcribe(
                audio_path,
                language=self.language
            )
            
            text = result["text"]
            
            logger.info(f"✅ Transkription erfolgreich: {len(text)} Zeichen")
            return text.strip()
        except Exception as e:
            logger.error(f"❌ Transkriptions-Fehler: {e}")
            return ""
    
    async def transcribe_bytes(self, audio_bytes: bytes, filename: str = "audio.wav") -> str:
        """Audio aus Bytes transkribieren"""
        # Temporäre Datei erstellen
        with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        
        try:
            return await self.transcribe_file(tmp_path)
        finally:
            # Temporäre Datei löschen
            Path(tmp_path).unlink(missing_ok=True)
