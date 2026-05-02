"""
Whisper Service - Speech to Text (faster-whisper / CTranslate2 backend)

Replaces the previous `openai-whisper` library with `faster-whisper` for ~4x
GPU throughput and clean device/compute_type configuration. The public method
signatures (`transcribe_file`, `transcribe_bytes`, `transcribe_with_speaker`,
`transcribe_bytes_with_speaker`) are unchanged so callers don't need updates.

Includes optional audio preprocessing for better transcription quality:
- Noise reduction (removes background noise like fans, AC)
- Audio normalization (consistent volume levels)
"""
import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

from faster_whisper import WhisperModel
from loguru import logger

from utils.config import settings

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
        self.device = settings.whisper_device
        self.compute_type = settings.whisper_compute_type
        self.beam_size = settings.whisper_beam_size
        self.model: WhisperModel | None = None
        self.language = settings.default_language
        self.initial_prompt = settings.whisper_initial_prompt or None

        # Bound concurrent transcriptions. faster-whisper / CTranslate2 is
        # thread-safe at the model level, so this gates submission rather than
        # the model itself — protects against OOM under multi-satellite bursts.
        self._max_concurrent = max(1, int(settings.whisper_max_concurrent))
        self._inflight_sem: asyncio.Semaphore | None = None

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

    def _get_semaphore(self) -> asyncio.Semaphore:
        """Lazily create the semaphore on the running loop's first call.

        Constructing it in __init__ would bind it to whichever loop is current
        at import time — typically none — making it brittle in tests and on
        worker restart. Lazy creation defers binding to the first transcribe
        call, which is always inside the serving loop.
        """
        if self._inflight_sem is None:
            self._inflight_sem = asyncio.Semaphore(self._max_concurrent)
        return self._inflight_sem

    def load_model(self):
        """Modell laden"""
        if self.model is None:
            try:
                logger.info(
                    f"📥 Lade Whisper Modell '{self.model_size}' "
                    f"(device={self.device}, compute_type={self.compute_type})..."
                )

                self.model = WhisperModel(
                    self.model_size,
                    device=self.device,
                    compute_type=self.compute_type,
                )

                logger.info("✅ Whisper Modell geladen")
            except Exception as e:
                logger.error(f"❌ Fehler beim Laden des Whisper Modells: {e}")
                raise

    def _run_transcription(self, transcribe_path: str, language: str, initial_prompt: str | None = None) -> str:
        """
        Run faster-whisper transcribe and concatenate the segment stream.

        faster-whisper returns (segments_iter, info). The iterator must be
        drained to actually run inference; converting to a list is the
        cleanest way to get the full text in one place.

        `initial_prompt` overrides the service-level default for this call
        only — used by the per-household bias hook to pass a prompt derived
        from the active speaker / room / KB.
        """
        kwargs = {
            "language": language,
            "beam_size": self.beam_size,
        }
        prompt = initial_prompt if initial_prompt is not None else self.initial_prompt
        if prompt:
            kwargs["initial_prompt"] = prompt

        segments, _info = self.model.transcribe(transcribe_path, **kwargs)
        return "".join(segment.text for segment in segments).strip()

    async def _transcribe_async(self, transcribe_path: str, language: str, initial_prompt: str | None = None) -> str:
        """Run sync `_run_transcription` off the event loop, gated by the semaphore."""
        async with self._get_semaphore():
            return await asyncio.to_thread(
                self._run_transcription, transcribe_path, language, initial_prompt
            )

    async def _extract_embedding_async(self, audio_path: str):
        """Run sync ECAPA-TDNN embedding extraction off the event loop.

        Returns the 192-dim numpy array, or None if speaker recognition is
        unavailable (SpeechBrain missing) or the audio is too short. Wrapped
        in `to_thread` so the speaker-recognition stage of the STT pipeline
        runs in parallel with `_transcribe_async` rather than serializing
        after it (Phase B-3 latency win — speaker_id ready ~50-150 ms earlier).
        """
        from services.speaker_service import get_speaker_service

        service = get_speaker_service()
        if not service.is_available():
            return None
        return await asyncio.to_thread(service.extract_embedding, audio_path)

    async def transcribe_file(self, audio_path: str, language: str = None, initial_prompt: str | None = None) -> str:
        """
        Audio-Datei transkribieren mit optionalem Preprocessing.

        Args:
            audio_path: Path to the audio file
            language: Optional language code (e.g., 'de', 'en'). Falls back to default_language.
            initial_prompt: Per-request bias string (overrides whisper_initial_prompt default).
        """
        if self.model is None:
            self.load_model()

        # Use provided language or fall back to default
        transcribe_language = language or self.language

        processed_path = None
        try:
            # Optional: Preprocess audio for better quality
            transcribe_path = audio_path
            if self.preprocess_enabled:
                processed_path = self._preprocess_audio(audio_path)
                if processed_path:
                    transcribe_path = processed_path
                    logger.info("📊 Using preprocessed audio")

            text = await self._transcribe_async(transcribe_path, transcribe_language, initial_prompt)

            logger.info(f"✅ Transkription erfolgreich ({transcribe_language}): {len(text)} Zeichen")
            return text
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

    async def transcribe_bytes(self, audio_bytes: bytes, filename: str = "audio.wav", language: str = None, initial_prompt: str | None = None) -> str:
        """
        Audio aus Bytes transkribieren.

        Args:
            audio_bytes: Raw audio bytes
            filename: Original filename (used for extension)
            language: Optional language code (e.g., 'de', 'en'). Falls back to default_language.
            initial_prompt: Per-request bias string (overrides whisper_initial_prompt default).
        """
        # Temporäre Datei erstellen
        with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            return await self.transcribe_file(tmp_path, language=language, initial_prompt=initial_prompt)
        finally:
            # Temporäre Datei löschen
            Path(tmp_path).unlink(missing_ok=True)

    async def transcribe_with_speaker(
        self,
        audio_path: str,
        db_session=None,
        language: str = None,
        initial_prompt: str | None = None,
    ) -> dict:
        """
        Transcribe audio and identify speaker.

        Features:
        - Identifies known speakers
        - Auto-enrolls unknown speakers (if enabled)
        - Continuous learning: adds embeddings on each interaction (if enabled)
        - Uses preprocessed audio for both transcription AND speaker recognition

        Args:
            audio_path: Path to audio file
            db_session: Optional async database session for speaker lookup
            language: Optional language code (e.g., 'de', 'en'). Falls back to default_language.

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
        if self.model is None:
            self.load_model()

        # Use provided language or fall back to default
        transcribe_language = language or self.language

        # Preprocess audio FIRST (for both transcription and speaker recognition)
        processed_path = None
        transcribe_path = audio_path
        if self.preprocess_enabled:
            processed_path = self._preprocess_audio(audio_path)
            if processed_path:
                transcribe_path = processed_path
                logger.info("📊 Using preprocessed audio for transcription and speaker recognition")

        try:
            # Default speaker info
            speaker_info = {
                "speaker_id": None,
                "speaker_name": None,
                "speaker_alias": None,
                "speaker_confidence": 0.0,
                "is_new_speaker": False
            }

            do_speaker_recog = settings.speaker_recognition_enabled and db_session is not None

            # Run STT and speaker-embedding extraction in PARALLEL when speaker
            # recognition is enabled. Both are I/O-bound (model inference in
            # threads), so asyncio.gather lets them overlap. Net win: the
            # speaker-id pipeline completes ~50-150 ms before STT finishes,
            # so downstream consumers (notification routing, etc.) see the
            # identified speaker as soon as the turn ends rather than after
            # an additional sequential embedding extraction.
            if do_speaker_recog:
                text, embedding = await asyncio.gather(
                    self._transcribe_async(transcribe_path, transcribe_language, initial_prompt),
                    self._extract_embedding_async(transcribe_path),
                )
            else:
                text = await self._transcribe_async(transcribe_path, transcribe_language, initial_prompt)
                embedding = None

            logger.info(f"✅ Transkription erfolgreich ({transcribe_language}): {len(text)} Zeichen")

            # Bail out if speaker recog disabled or unavailable.
            if not do_speaker_recog:
                return {"text": text, **speaker_info}
            if embedding is None:
                logger.debug("Speaker embedding unavailable (recog disabled, audio too short, or backend missing)")
                return {"text": text, **speaker_info}

            try:
                import numpy as np
                from sqlalchemy import select
                from sqlalchemy.orm import selectinload

                from models.database import Speaker, SpeakerEmbedding
                from services.speaker_service import get_speaker_service

                service = get_speaker_service()

                # Load ALL speakers (including those without embeddings for counting)
                result = await db_session.execute(
                    select(Speaker).options(selectinload(Speaker.embeddings))
                )
                all_speakers = result.scalars().all()

                # Build list of speakers WITH embeddings for identification
                # Limit to most recent 10 embeddings per speaker to avoid loading
                # unbounded data as continuous learning adds more embeddings over time
                MAX_EMBEDDINGS_PER_SPEAKER = 10
                known_speakers = []
                speakers_with_embeddings = []
                for speaker in all_speakers:
                    if speaker.embeddings:
                        speakers_with_embeddings.append(speaker)
                        recent_embeddings = sorted(
                            speaker.embeddings,
                            key=lambda e: e.created_at or datetime.min,
                            reverse=True
                        )[:MAX_EMBEDDINGS_PER_SPEAKER]
                        embeddings = [
                            service.embedding_from_base64(emb.embedding)
                            for emb in recent_embeddings
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
                        speaker_id, _speaker_name, confidence = result
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

        except Exception as e:
            logger.error(f"❌ Transkriptions-Fehler: {e}")
            return {"text": "", "speaker_id": None, "speaker_name": None, "speaker_alias": None, "speaker_confidence": 0.0, "is_new_speaker": False}

        finally:
            # Cleanup preprocessed temp file
            if processed_path and processed_path != audio_path:
                try:
                    Path(processed_path).unlink(missing_ok=True)
                except Exception:
                    pass

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
            from sqlalchemy import func, select

            from models.database import SpeakerEmbedding

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
        db_session=None,
        language: str = None,
        initial_prompt: str | None = None,
    ) -> dict:
        """
        Transcribe audio bytes and identify speaker.

        Args:
            audio_bytes: Raw audio bytes
            filename: Original filename
            db_session: Optional async database session
            language: Optional language code (e.g., 'de', 'en'). Falls back to default_language.
            initial_prompt: Per-request bias string.

        Returns:
            Same as transcribe_with_speaker
        """
        with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            return await self.transcribe_with_speaker(tmp_path, db_session, language=language, initial_prompt=initial_prompt)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
