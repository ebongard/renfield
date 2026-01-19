"""
Whisper Service - Speech to Text

Includes optional audio preprocessing for better transcription quality:
- Noise reduction (removes background noise like fans, AC)
- Audio normalization (consistent volume levels)
"""
import whisper
from pathlib import Path
from loguru import logger
from utils.config import settings
import tempfile

# Optional: librosa and soundfile for audio preprocessing
try:
    import librosa
    import soundfile as sf
    LIBROSA_AVAILABLE = True
except ImportError:
    LIBROSA_AVAILABLE = False
    logger.warning("librosa/soundfile not available - audio preprocessing disabled. Install with: pip install librosa soundfile")

from services.audio_preprocessor import AudioPreprocessor


class WhisperService:
    """Service für Speech-to-Text mit Whisper"""

    def __init__(self):
        self.model_size = settings.whisper_model
        self.model = None
        self.language = settings.default_language
        self.initial_prompt = settings.whisper_initial_prompt or None

        # Audio Preprocessor (for noise reduction and normalization)
        self.preprocessor = AudioPreprocessor(
            sample_rate=16000,
            noise_reduce_enabled=settings.whisper_preprocess_noise_reduce,
            normalize_enabled=settings.whisper_preprocess_normalize,
            target_db=settings.whisper_preprocess_target_db
        )
        self.preprocess_enabled = settings.whisper_preprocess_enabled and LIBROSA_AVAILABLE

        if settings.whisper_preprocess_enabled and not LIBROSA_AVAILABLE:
            logger.warning("Audio preprocessing requested but librosa not installed")
    
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
        """Audio-Datei transkribieren mit optionalem Preprocessing"""
        if self.model is None:
            self.load_model()

        processed_path = None
        try:
            # Optional: Preprocess audio for better quality
            transcribe_path = audio_path
            if self.preprocess_enabled:
                processed_path = self._preprocess_audio(audio_path)
                if processed_path:
                    transcribe_path = processed_path
                    logger.info("📊 Using preprocessed audio")

            # Transkribieren mit OpenAI Whisper
            # fp16=False verhindert die Warnung auf CPU-only Systemen
            # beam_size=5 und best_of=5 für bessere Genauigkeit
            transcribe_opts = {
                "language": self.language,
                "fp16": False,
                "beam_size": 5,
                "best_of": 5,
            }
            if self.initial_prompt:
                transcribe_opts["initial_prompt"] = self.initial_prompt

            result = self.model.transcribe(transcribe_path, **transcribe_opts)

            text = result["text"]

            logger.info(f"✅ Transkription erfolgreich: {len(text)} Zeichen")
            return text.strip()
        except Exception as e:
            logger.error(f"❌ Transkriptions-Fehler: {e}")
            return ""
        finally:
            # Cleanup preprocessed temp file
            if processed_path and processed_path != audio_path:
                try:
                    Path(processed_path).unlink(missing_ok=True)
                except Exception:
                    pass

    def _preprocess_audio(self, audio_path: str) -> str:
        """
        Preprocess audio file for better transcription quality.

        Loads audio, applies noise reduction and normalization,
        then saves to a temporary WAV file.

        Args:
            audio_path: Path to original audio file

        Returns:
            Path to preprocessed audio file, or None if preprocessing failed
        """
        if not LIBROSA_AVAILABLE:
            return None

        try:
            # Load audio (auto-converts format, resamples to 16kHz mono)
            # librosa handles: WAV, MP3, FLAC, OGG, WebM, etc.
            # Note: WebM files require audioread/ffmpeg backend (soundfile doesn't support WebM)
            import warnings
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="PySoundFile failed")
                warnings.filterwarnings("ignore", category=FutureWarning)
                audio, sr = librosa.load(audio_path, sr=16000, mono=True)

            logger.debug(f"📊 Audio loaded: {len(audio)} samples ({len(audio)/16000:.2f}s), {sr}Hz")

            # Apply preprocessing (noise reduction + normalization)
            processed = self.preprocessor.process(audio)

            # Save to temp WAV file
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
                sf.write(tmp.name, processed, 16000)
                logger.debug(f"✅ Preprocessed audio saved: {tmp.name}")
                return tmp.name

        except Exception as e:
            logger.warning(f"⚠️ Preprocessing failed, using original audio: {e}")
            return None
    
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
