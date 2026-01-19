"""
Speaker Recognition Service

Uses SpeechBrain ECAPA-TDNN for speaker embedding extraction and verification.
Provides speaker identification, verification, and enrollment capabilities.
"""
import numpy as np
from typing import Optional, List, Tuple
from loguru import logger
from pathlib import Path
import tempfile
import base64

from utils.config import settings

# Lazy imports for optional dependencies
SPEECHBRAIN_AVAILABLE = False
SPEECHBRAIN_ERROR = None
try:
    import torch
    import torchaudio

    # Workaround for torchaudio 2.1+ where list_audio_backends() was removed
    # SpeechBrain's check_torchaudio_backend() calls this deprecated function
    if not hasattr(torchaudio, 'list_audio_backends'):
        # Provide a dummy implementation to satisfy SpeechBrain's check
        torchaudio.list_audio_backends = lambda: ['soundfile', 'sox']

        # Also patch get_audio_backend if needed
        if not hasattr(torchaudio, 'get_audio_backend'):
            torchaudio.get_audio_backend = lambda: 'soundfile'

    from speechbrain.inference.speaker import EncoderClassifier
    SPEECHBRAIN_AVAILABLE = True
    logger.info("✅ SpeechBrain speaker recognition available")
except ImportError as e:
    SPEECHBRAIN_ERROR = str(e)
    logger.warning(
        f"SpeechBrain not available (ImportError): {e}. "
        "Install with: pip install speechbrain torchaudio"
    )
except AttributeError as e:
    SPEECHBRAIN_ERROR = str(e)
    logger.warning(
        f"SpeechBrain initialization failed (AttributeError): {e}. "
        "This is likely a torchaudio version incompatibility."
    )
except Exception as e:
    SPEECHBRAIN_ERROR = str(e)
    logger.warning(f"SpeechBrain not available: {e}")


class SpeakerService:
    """Service for speaker recognition and verification using SpeechBrain ECAPA-TDNN"""

    def __init__(
        self,
        model_source: str = "speechbrain/spkrec-ecapa-voxceleb",
        device: Optional[str] = None,
        similarity_threshold: Optional[float] = None
    ):
        """
        Initialize the speaker service.

        Args:
            model_source: HuggingFace model identifier
            device: "cpu" or "cuda" (defaults to config setting)
            similarity_threshold: Minimum similarity for positive identification
        """
        self.model_source = model_source
        self.device = device or getattr(settings, 'speaker_recognition_device', 'cpu')
        self.similarity_threshold = similarity_threshold or getattr(
            settings, 'speaker_recognition_threshold', 0.25
        )
        self.encoder = None
        self._model_loaded = False

        if not SPEECHBRAIN_AVAILABLE:
            logger.error("SpeechBrain not installed - speaker recognition disabled")

    def is_available(self) -> bool:
        """Check if speaker recognition is available"""
        return SPEECHBRAIN_AVAILABLE

    def load_model(self):
        """Load the speaker embedding model (lazy loading)"""
        if self._model_loaded:
            return

        if not SPEECHBRAIN_AVAILABLE:
            raise RuntimeError("SpeechBrain not available")

        logger.info(f"📥 Loading speaker embedding model: {self.model_source}")
        logger.info(f"   Device: {self.device}")

        try:
            self.encoder = EncoderClassifier.from_hparams(
                source=self.model_source,
                run_opts={"device": self.device}
            )
            self._model_loaded = True
            logger.info("✅ Speaker embedding model loaded")
        except Exception as e:
            logger.error(f"❌ Failed to load speaker model: {e}")
            raise

    def extract_embedding(self, audio_path: str) -> Optional[np.ndarray]:
        """
        Extract speaker embedding from audio file.

        Args:
            audio_path: Path to audio file (WAV, MP3, FLAC, WebM, etc.)

        Returns:
            192-dimensional embedding vector or None on error
        """
        if not SPEECHBRAIN_AVAILABLE:
            logger.warning("SpeechBrain not available, cannot extract embedding")
            return None

        self.load_model()

        try:
            # Use librosa for audio loading (handles WebM, MP3, etc. better than torchaudio)
            import librosa
            import warnings

            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="PySoundFile failed")
                warnings.filterwarnings("ignore", category=FutureWarning)
                # Load audio, resample to 16kHz mono
                audio_np, sr = librosa.load(audio_path, sr=16000, mono=True)

            # Check minimum duration (at least 0.5 seconds)
            min_samples = int(0.5 * 16000)
            if len(audio_np) < min_samples:
                logger.warning(f"Audio too short for speaker embedding: {len(audio_np)} samples")
                return None

            # Convert to torch tensor with shape (1, samples)
            signal = torch.from_numpy(audio_np).unsqueeze(0).float()

            # Extract embedding
            with torch.no_grad():
                embedding = self.encoder.encode_batch(signal)

            embedding_np = embedding.squeeze().cpu().numpy()
            logger.debug(f"Extracted embedding: shape={embedding_np.shape}")

            return embedding_np

        except Exception as e:
            logger.error(f"Failed to extract embedding: {e}")
            return None

    def extract_embedding_from_bytes(
        self,
        audio_bytes: bytes,
        filename: str = "audio.wav"
    ) -> Optional[np.ndarray]:
        """
        Extract embedding from audio bytes.

        Args:
            audio_bytes: Raw audio file bytes
            filename: Original filename (for format detection)

        Returns:
            Embedding vector or None on error
        """
        # Determine suffix from filename
        suffix = Path(filename).suffix or '.wav'

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            return self.extract_embedding(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def compute_similarity(
        self,
        embedding1: np.ndarray,
        embedding2: np.ndarray
    ) -> float:
        """
        Compute cosine similarity between two embeddings.

        Args:
            embedding1: First embedding vector
            embedding2: Second embedding vector

        Returns:
            Similarity score between -1 and 1 (higher = more similar)
        """
        # Normalize embeddings
        norm1 = np.linalg.norm(embedding1)
        norm2 = np.linalg.norm(embedding2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        normalized1 = embedding1 / norm1
        normalized2 = embedding2 / norm2

        # Cosine similarity
        return float(np.dot(normalized1, normalized2))

    def identify_speaker(
        self,
        query_embedding: np.ndarray,
        known_speakers: List[Tuple[int, str, np.ndarray]]
    ) -> Optional[Tuple[int, str, float]]:
        """
        Identify speaker from a list of known speakers.

        Args:
            query_embedding: Embedding of the audio to identify
            known_speakers: List of (speaker_id, speaker_name, averaged_embedding) tuples

        Returns:
            (speaker_id, speaker_name, confidence) or None if no match above threshold
        """
        if not known_speakers:
            return None

        best_match_id = None
        best_match_name = None
        best_score = -1.0

        for speaker_id, speaker_name, known_embedding in known_speakers:
            score = self.compute_similarity(query_embedding, known_embedding)

            if score > best_score:
                best_score = score
                best_match_id = speaker_id
                best_match_name = speaker_name

        logger.debug(f"Best match: {best_match_name} with score {best_score:.3f}")

        if best_score >= self.similarity_threshold:
            return (best_match_id, best_match_name, best_score)

        logger.debug(f"No match above threshold {self.similarity_threshold}")
        return None

    def verify_speaker(
        self,
        query_embedding: np.ndarray,
        claimed_embeddings: List[np.ndarray]
    ) -> Tuple[bool, float]:
        """
        Verify if embedding matches claimed speaker.

        Args:
            query_embedding: Embedding to verify
            claimed_embeddings: List of embeddings for the claimed speaker

        Returns:
            (is_verified, confidence_score)
        """
        if not claimed_embeddings:
            return (False, 0.0)

        # Calculate similarity against all claimed embeddings
        scores = [
            self.compute_similarity(query_embedding, ref)
            for ref in claimed_embeddings
        ]

        # Use average score for verification
        avg_score = float(np.mean(scores))

        is_verified = avg_score >= self.similarity_threshold
        return (is_verified, avg_score)

    @staticmethod
    def embedding_to_base64(embedding: np.ndarray) -> str:
        """Serialize embedding to base64 string for database storage"""
        return base64.b64encode(embedding.astype(np.float32).tobytes()).decode('utf-8')

    @staticmethod
    def embedding_from_base64(encoded: str) -> np.ndarray:
        """Deserialize embedding from base64 string"""
        return np.frombuffer(base64.b64decode(encoded), dtype=np.float32)


# Global service instance (lazy initialization)
_speaker_service: Optional[SpeakerService] = None


def get_speaker_service() -> SpeakerService:
    """Get or create the global speaker service instance"""
    global _speaker_service

    if _speaker_service is None:
        enabled = getattr(settings, 'speaker_recognition_enabled', True)
        if enabled:
            _speaker_service = SpeakerService()
        else:
            logger.info("Speaker recognition is disabled in config")
            _speaker_service = SpeakerService()  # Create but won't load model

    return _speaker_service
