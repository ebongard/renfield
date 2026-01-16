"""
Piper Service - Text to Speech
"""
from pathlib import Path
from loguru import logger
from utils.config import settings
import subprocess
import tempfile

class PiperService:
    """Service für Text-to-Speech mit Piper"""
    
    def __init__(self):
        self.voice = settings.piper_voice
        # Konvertiere Voice-Namen zu Model-Pfad
        self.model_path = f"/usr/share/piper/voices/{self.voice}.onnx"
        self.available = self._check_piper_available()
    
    def _check_piper_available(self) -> bool:
        """Prüfe ob Piper verfügbar ist"""
        try:
            result = subprocess.run(
                ["which", "piper"],
                capture_output=True,
                text=True
            )
            available = result.returncode == 0
            if not available:
                logger.warning("⚠️  Piper TTS nicht verfügbar. TTS-Funktionen sind deaktiviert.")
                logger.info("💡 Installation: pip install piper-tts")
            else:
                logger.info("✅ Piper TTS verfügbar")
            return available
        except Exception as e:
            logger.warning(f"⚠️  Piper TTS Check fehlgeschlagen: {e}")
            return False
    
    def ensure_model_downloaded(self):
        """Stelle sicher, dass das Sprachmodell heruntergeladen ist"""
        if not self.available:
            logger.warning("Piper nicht verfügbar, überspringe Model-Download")
            return
        # Piper lädt Modelle automatisch beim ersten Aufruf
        pass
    
    async def synthesize_to_file(self, text: str, output_path: str) -> bool:
        """Text zu Audio-Datei synthetisieren"""
        if not self.available:
            logger.warning("Piper nicht verfügbar, TTS übersprungen")
            return False
            
        try:
            # Piper CLI aufrufen
            cmd = [
                "piper",
                "--model", self.model_path,
                "--output_file", output_path
            ]
            
            # Text über stdin übergeben
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            stdout, stderr = process.communicate(input=text.encode('utf-8'))
            
            if process.returncode == 0:
                logger.info(f"✅ TTS erfolgreich: {output_path}")
                return True
            else:
                logger.error(f"❌ Piper Fehler: {stderr.decode()}")
                return False
        except Exception as e:
            logger.error(f"❌ TTS Fehler: {e}")
            return False
    
    async def synthesize_to_bytes(self, text: str) -> bytes:
        """Text zu Audio-Bytes synthetisieren"""
        if not self.available:
            logger.warning("Piper nicht verfügbar, TTS übersprungen")
            return b""
            
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        
        try:
            success = await self.synthesize_to_file(text, tmp_path)
            if success:
                with open(tmp_path, "rb") as f:
                    return f.read()
            return b""
        finally:
            Path(tmp_path).unlink(missing_ok=True)
