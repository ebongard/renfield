"""
Piper Service - Text to Speech (in-process Python bindings)

Uses the `piper-tts` Python package directly instead of shelling out to the
`piper` CLI per request. Eliminates the ~150-300 ms subprocess cold-start
that the previous implementation paid on every TTS call.

Voice models still live at `/usr/share/piper/voices/<voice>.onnx` (downloaded
by the Dockerfile) — we just load them in-process via PiperVoice.load() and
cache the loaded voice across requests.

Configuration via PIPER_VOICES environment variable:
  PIPER_VOICES=de:de_DE-thorsten-high,en:en_US-amy-medium
"""
import io
import wave
from pathlib import Path

from loguru import logger

from utils.config import settings

try:
    from piper.voice import PiperVoice
    PIPER_AVAILABLE = True
except ImportError:
    PIPER_AVAILABLE = False
    PiperVoice = None  # type: ignore[assignment,misc]


class PiperService:
    """Service für Text-to-Speech mit Piper (in-process)."""

    def __init__(self):
        self.default_voice = settings.piper_default_voice
        self.voice_map = settings.piper_voice_map
        self.default_language = settings.default_language
        self.available = PIPER_AVAILABLE
        # Cache loaded PiperVoice instances by voice name. Each voice is ~50-100 MB
        # in memory (ONNX session + tokenizer); for the typical de/en pair we hold
        # ~200 MB total which is fine.
        self._voice_cache: dict[str, "PiperVoice"] = {}

        if not PIPER_AVAILABLE:
            logger.warning(
                "⚠️  piper-tts Python package not installed. TTS-Funktionen sind deaktiviert. "
                "Install with: pip install piper-tts>=1.2.0"
            )
        else:
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

    def _load_voice(self, voice_name: str) -> "PiperVoice | None":
        """
        Load a PiperVoice from disk, with caching.

        Returns None if the model file is missing or fails to load — caller
        falls back to a no-op (matching previous behavior when piper CLI was
        absent).
        """
        if not PIPER_AVAILABLE:
            return None
        cached = self._voice_cache.get(voice_name)
        if cached is not None:
            return cached
        model_path = self._get_model_path(voice_name)
        if not Path(model_path).exists():
            logger.error(f"❌ Piper voice model not found: {model_path}")
            return None
        try:
            voice = PiperVoice.load(model_path)
            self._voice_cache[voice_name] = voice
            logger.info(f"📥 Piper voice geladen: {voice_name}")
            return voice
        except Exception as e:
            logger.error(f"❌ Fehler beim Laden der Piper voice {voice_name}: {e}")
            return None

    def ensure_model_downloaded(self):
        """Stelle sicher, dass das Sprachmodell heruntergeladen ist."""
        # Voice models are pre-baked into the Docker image at /usr/share/piper/voices/.
        # In-process load happens lazily on first synthesis call via _load_voice().
        return

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

        voice_name = self._get_voice_for_language(language)
        voice = self._load_voice(voice_name)
        if voice is None:
            return False

        try:
            with wave.open(output_path, "wb") as wav_file:
                voice.synthesize(text, wav_file)
            logger.info(f"✅ TTS erfolgreich ({voice_name}): {output_path}")
            return True
        except Exception as e:
            logger.error(f"❌ TTS Fehler ({voice_name}): {e}")
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

        voice_name = self._get_voice_for_language(language)
        voice = self._load_voice(voice_name)
        if voice is None:
            return b""

        try:
            buffer = io.BytesIO()
            # `wave.open` requires a mode string; PiperVoice.synthesize writes
            # frames into the wave object directly.
            with wave.open(buffer, "wb") as wav_file:
                voice.synthesize(text, wav_file)
            return buffer.getvalue()
        except Exception as e:
            logger.error(f"❌ TTS Fehler ({voice_name}): {e}")
            return b""


_piper_instance: PiperService | None = None


def get_piper_service() -> PiperService:
    """Get the PiperService singleton."""
    global _piper_instance
    if _piper_instance is None:
        _piper_instance = PiperService()
    return _piper_instance
