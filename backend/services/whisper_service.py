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

    async def transcribe_with_speaker(
        self,
        audio_path: str,
        db_session=None
    ) -> dict:
        """
        Transcribe audio and identify speaker.

        Features:
        - Identifies known speakers
        - Auto-enrolls unknown speakers (if enabled)
        - Continuous learning: adds embeddings on each interaction (if enabled)

        Args:
            audio_path: Path to audio file
            db_session: Optional async database session for speaker lookup

        Returns:
            {
                "text": "transcribed text",
                "speaker_id": int or None,
                "speaker_name": str or None,
                "speaker_alias": str or None,
                "speaker_confidence": float (0-1),
                "is_new_speaker": bool
            }
        """
        # Transcribe audio
        text = await self.transcribe_file(audio_path)

        # Default speaker info
        speaker_info = {
            "speaker_id": None,
            "speaker_name": None,
            "speaker_alias": None,
            "speaker_confidence": 0.0,
            "is_new_speaker": False
        }

        # Try to identify speaker if enabled and db_session provided
        if not settings.speaker_recognition_enabled or db_session is None:
            return {"text": text, **speaker_info}

        try:
            from services.speaker_service import get_speaker_service
            from models.database import Speaker, SpeakerEmbedding
            from sqlalchemy import select, func
            from sqlalchemy.orm import selectinload
            import numpy as np

            service = get_speaker_service()

            if not service.is_available():
                logger.debug("Speaker recognition not available")
                return {"text": text, **speaker_info}

            # Extract embedding from audio
            embedding = service.extract_embedding(audio_path)

            if embedding is None:
                logger.debug("Could not extract speaker embedding")
                return {"text": text, **speaker_info}

            # Load ALL speakers (including those without embeddings for counting)
            result = await db_session.execute(
                select(Speaker).options(selectinload(Speaker.embeddings))
            )
            all_speakers = result.scalars().all()

            # Build list of speakers WITH embeddings for identification
            known_speakers = []
            speakers_with_embeddings = []
            for speaker in all_speakers:
                if speaker.embeddings:
                    speakers_with_embeddings.append(speaker)
                    embeddings = [
                        service.embedding_from_base64(emb.embedding)
                        for emb in speaker.embeddings
                    ]
                    if embeddings:
                        averaged = np.mean(embeddings, axis=0)
                        known_speakers.append((speaker.id, speaker.name, averaged))

            # Try to identify speaker
            identified_speaker = None
            confidence = 0.0

            if known_speakers:
                result = service.identify_speaker(embedding, known_speakers)
                if result:
                    speaker_id, speaker_name, confidence = result
                    # Find the speaker object
                    for speaker in speakers_with_embeddings:
                        if speaker.id == speaker_id:
                            identified_speaker = speaker
                            break

            # Case 1: Speaker identified
            if identified_speaker:
                speaker_info = {
                    "speaker_id": identified_speaker.id,
                    "speaker_name": identified_speaker.name,
                    "speaker_alias": identified_speaker.alias,
                    "speaker_confidence": confidence,
                    "is_new_speaker": False
                }
                logger.info(f"🎤 Speaker identified: {identified_speaker.name} ({confidence:.2f})")

                # Continuous learning: add embedding to known speaker
                if settings.speaker_continuous_learning:
                    await self._add_embedding_to_speaker(
                        db_session, identified_speaker.id, embedding, service
                    )

            # Case 2: No speaker identified - auto-enroll if enabled
            elif settings.speaker_auto_enroll:
                # Count existing "Unbekannter Sprecher" entries
                unknown_count = sum(
                    1 for s in all_speakers
                    if s.name.startswith("Unbekannter Sprecher")
                )
                new_number = unknown_count + 1

                # Create new unknown speaker
                new_speaker = Speaker(
                    name=f"Unbekannter Sprecher #{new_number}",
                    alias=f"unknown_{new_number}",
                    is_admin=False
                )
                db_session.add(new_speaker)
                await db_session.flush()  # Get the ID

                # Add embedding
                embedding_record = SpeakerEmbedding(
                    speaker_id=new_speaker.id,
                    embedding=service.embedding_to_base64(embedding)
                )
                db_session.add(embedding_record)
                await db_session.commit()

                speaker_info = {
                    "speaker_id": new_speaker.id,
                    "speaker_name": new_speaker.name,
                    "speaker_alias": new_speaker.alias,
                    "speaker_confidence": 1.0,  # It's a new profile, 100% match to itself
                    "is_new_speaker": True
                }
                logger.info(f"🆕 New unknown speaker created: {new_speaker.name} (ID: {new_speaker.id})")

            else:
                logger.info("🎤 Speaker not recognized (auto-enroll disabled)")

        except Exception as e:
            logger.warning(f"Speaker identification failed: {e}")
            import traceback
            logger.debug(traceback.format_exc())

        return {"text": text, **speaker_info}

    async def _add_embedding_to_speaker(
        self,
        db_session,
        speaker_id: int,
        embedding,
        service
    ):
        """
        Add embedding to existing speaker for continuous learning.

        Limits to max 10 embeddings per speaker to prevent unbounded growth.
        """
        try:
            from models.database import Speaker, SpeakerEmbedding
            from sqlalchemy import select, func
            from sqlalchemy.orm import selectinload

            # Check current embedding count
            result = await db_session.execute(
                select(func.count(SpeakerEmbedding.id))
                .where(SpeakerEmbedding.speaker_id == speaker_id)
            )
            count = result.scalar()

            # Limit to 10 embeddings per speaker
            if count >= 10:
                logger.debug(f"Speaker {speaker_id} already has {count} embeddings, skipping")
                return

            # Add new embedding
            embedding_record = SpeakerEmbedding(
                speaker_id=speaker_id,
                embedding=service.embedding_to_base64(embedding)
            )
            db_session.add(embedding_record)
            await db_session.commit()

            logger.debug(f"📊 Added embedding to speaker {speaker_id} (now {count + 1} total)")

        except Exception as e:
            logger.warning(f"Failed to add embedding for continuous learning: {e}")

    async def transcribe_bytes_with_speaker(
        self,
        audio_bytes: bytes,
        filename: str = "audio.wav",
        db_session=None
    ) -> dict:
        """
        Transcribe audio bytes and identify speaker.

        Args:
            audio_bytes: Raw audio bytes
            filename: Original filename
            db_session: Optional async database session

        Returns:
            Same as transcribe_with_speaker
        """
        with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            return await self.transcribe_with_speaker(tmp_path, db_session)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
