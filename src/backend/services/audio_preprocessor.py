"""
Audio Preprocessing Service
Noise reduction and normalization for better STT quality

This module provides audio preprocessing capabilities that can be applied
to any audio input (web, satellite, API) before Whisper transcription.
"""

import numpy as np
from loguru import logger

# Optional: noisereduce (pip install noisereduce)
try:
    import noisereduce as nr
    NOISEREDUCE_AVAILABLE = True
except ImportError:
    NOISEREDUCE_AVAILABLE = False
    logger.warning("noisereduce not available - noise reduction disabled. Install with: pip install noisereduce")


class AudioPreprocessor:
    """Audio preprocessing for improved Whisper transcription"""

    def __init__(
        self,
        sample_rate: int = 16000,
        noise_reduce_enabled: bool = True,
        normalize_enabled: bool = True,
        target_db: float = -20.0
    ):
        """
        Initialize the audio preprocessor.

        Args:
            sample_rate: Audio sample rate (default: 16000 for Whisper)
            noise_reduce_enabled: Enable spectral noise reduction
            normalize_enabled: Enable audio level normalization
            target_db: Target dB level for normalization (default: -20.0)
        """
        self.sample_rate = sample_rate
        self.noise_reduce_enabled = noise_reduce_enabled and NOISEREDUCE_AVAILABLE
        self.normalize_enabled = normalize_enabled
        self.target_db = target_db

        if noise_reduce_enabled and not NOISEREDUCE_AVAILABLE:
            logger.warning("Noise reduction requested but noisereduce not installed")

        logger.info(
            f"AudioPreprocessor initialized: "
            f"noise_reduce={self.noise_reduce_enabled}, "
            f"normalize={self.normalize_enabled}, "
            f"target_db={self.target_db}"
        )

    def normalize(self, audio: np.ndarray, target_db: float | None = None) -> np.ndarray:
        """
        Normalize audio to target dB level.

        This helps with quiet speakers or distant microphones by bringing
        the audio level to a consistent target.

        Args:
            audio: Audio data as numpy array (float, -1.0 to 1.0)
            target_db: Target dB level (optional, uses instance default)

        Returns:
            Normalized audio array
        """
        if target_db is None:
            target_db = self.target_db

        # Calculate RMS (Root Mean Square)
        rms = np.sqrt(np.mean(audio ** 2))

        if rms > 0:
            # Target RMS for given dB level (relative to full scale)
            target_rms = 10 ** (target_db / 20)
            # Scale audio to reach target RMS
            audio = audio * (target_rms / rms)

        # Clip to prevent overflow
        return np.clip(audio, -1.0, 1.0)

    def reduce_noise(self, audio: np.ndarray) -> np.ndarray:
        """
        Apply spectral noise reduction.

        Uses noisereduce library to remove stationary background noise
        like fans, AC, computer hum, etc.

        Args:
            audio: Audio data as numpy array (float, -1.0 to 1.0)

        Returns:
            Noise-reduced audio array
        """
        if not NOISEREDUCE_AVAILABLE:
            return audio

        try:
            reduced = nr.reduce_noise(
                y=audio,
                sr=self.sample_rate,
                stationary=True,      # Assume stationary noise (fans, AC)
                prop_decrease=0.75    # How much to reduce noise (0-1)
            )
            return reduced
        except Exception as e:
            logger.warning(f"Noise reduction failed: {e}")
            return audio

    def process(self, audio: np.ndarray) -> np.ndarray:
        """
        Apply all preprocessing steps to audio.

        The order is: noise reduction first, then normalization.
        This ensures that noise is reduced before adjusting levels.

        Args:
            audio: Audio data as numpy array (float, -1.0 to 1.0)

        Returns:
            Preprocessed audio array
        """
        original_rms = np.sqrt(np.mean(audio ** 2))

        # 1. Noise reduction (if enabled)
        if self.noise_reduce_enabled:
            audio = self.reduce_noise(audio)
            logger.debug("Applied noise reduction")

        # 2. Normalization (if enabled)
        if self.normalize_enabled:
            audio = self.normalize(audio)
            logger.debug(f"Normalized audio from RMS={original_rms:.4f} to target={self.target_db}dB")

        return audio
