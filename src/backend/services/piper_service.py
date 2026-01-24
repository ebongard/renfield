"""
Piper Service - Text to Speech

Supports multiple voices based on language.
Configuration via PIPER_VOICES environment variable:
  PIPER_VOICES=de:de_DE-thorsten-high,en:en_US-amy-medium
"""
from pathlib import Path
from loguru import logger
from utils.config import settings
import subprocess
import tempfile
from typing import Optional


class PiperService:
    """Service für Text-to-Speech mit Piper"""

    def __init__(self):
        self.default_voice = settings.piper_voice
        self.voice_map = settings.piper_voice_map
        self.default_language = settings.default_language
        self.available = self._check_piper_available()

        # Log available voices
        if self.available:
            logger.info(f"🗣️ Piper voice map: {self.voice_map}")

    def _get_voice_for_language(self, language: str = None) -> str:
        """
        Get the voice name for a given language.

        Args:
            language: Language code (e.g., 'de', 'en'). Falls back to default_language.

        Returns:
            Voice name (e.g., 'de_DE-thorsten-high')
        """
        lang = (language or self.default_language).lower()
        return self.voice_map.get(lang, self.default_voice)

    def _get_model_path(self, voice: str) -> str:
        """Get the model path for a given voice."""
        return f"/usr/share/piper/voices/{voice}.onnx"
    
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

    async def synthesize_to_file(self, text: str, output_path: str, language: str = None) -> bool:
        """
        Text zu Audio-Datei synthetisieren.

        Args:
            text: Text to synthesize
            output_path: Path for output audio file
            language: Optional language code (e.g., 'de', 'en'). Falls back to default_language.
        """
        if not self.available:
            logger.warning("Piper nicht verfügbar, TTS übersprungen")
            return False

        # Get voice for the requested language
        voice = self._get_voice_for_language(language)
        model_path = self._get_model_path(voice)

        try:
            # Piper CLI aufrufen
            cmd = [
                "piper",
                "--model", model_path,
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
                logger.info(f"✅ TTS erfolgreich ({voice}): {output_path}")
                return True
            else:
                logger.error(f"❌ Piper Fehler ({voice}): {stderr.decode()}")
                return False
        except Exception as e:
            logger.error(f"❌ TTS Fehler: {e}")
            return False

    async def synthesize_to_bytes(self, text: str, language: str = None) -> bytes:
        """
        Text zu Audio-Bytes synthetisieren.

        Args:
            text: Text to synthesize
            language: Optional language code (e.g., 'de', 'en'). Falls back to default_language.
        """
        if not self.available:
            logger.warning("Piper nicht verfügbar, TTS übersprungen")
            return b""

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            success = await self.synthesize_to_file(text, tmp_path, language=language)
            if success:
                with open(tmp_path, "rb") as f:
                    return f.read()
            return b""
        finally:
            Path(tmp_path).unlink(missing_ok=True)
