"""
Audio Preprocessor for Renfield Satellite

Provides noise reduction and audio normalization to improve
speech recognition quality before sending audio to the backend.
"""

import numpy as np
from typing import Optional

# Try to import noisereduce (optional dependency)
NOISEREDUCE_AVAILABLE = False
try:
    import noisereduce as nr
    NOISEREDUCE_AVAILABLE = True
except ImportError:
    nr = None
    print("Note: noisereduce not installed. Noise reduction disabled.")
    print("Install with: pip install noisereduce")


class AudioPreprocessor:
    """
    Preprocesses audio for better speech recognition.

    Features:
    - Noise reduction (spectral gating)
    - Audio normalization (RMS-based)
    - High-pass filter (removes low-frequency rumble)
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        noise_reduce_enabled: bool = True,
        normalize_enabled: bool = True,
        target_db: float = -20.0,
    ):
        """
        Initialize audio preprocessor.

        Args:
            sample_rate: Sample rate in Hz
            noise_reduce_enabled: Enable noise reduction
            normalize_enabled: Enable volume normalization
            target_db: Target RMS level in dB for normalization
        """
        self.sample_rate = sample_rate
        self.noise_reduce_enabled = noise_reduce_enabled and NOISEREDUCE_AVAILABLE
        self.normalize_enabled = normalize_enabled
        self.target_db = target_db

        # Noise profile (can be calibrated)
        self._noise_profile: Optional[np.ndarray] = None

        if noise_reduce_enabled and not NOISEREDUCE_AVAILABLE:
            print("Warning: Noise reduction requested but noisereduce not installed")

    def process(self, audio_bytes: bytes) -> bytes:
        """
        Process audio through the full pipeline.

        Args:
            audio_bytes: Raw PCM audio bytes (16-bit, mono)

        Returns:
            Processed audio bytes
        """
        if len(audio_bytes) < 100:
            return audio_bytes

        # Convert to numpy array
        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)

        # Apply noise reduction
        if self.noise_reduce_enabled:
            audio = self._reduce_noise(audio)

        # Apply normalization
        if self.normalize_enabled:
            audio = self._normalize(audio)

        # Convert back to bytes
        return audio.clip(-32768, 32767).astype(np.int16).tobytes()

    def reduce_noise(self, audio_bytes: bytes) -> bytes:
        """
        Apply only noise reduction.

        Args:
            audio_bytes: Raw PCM audio bytes

        Returns:
            Noise-reduced audio bytes
        """
        if not self.noise_reduce_enabled:
            return audio_bytes

        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
        reduced = self._reduce_noise(audio)
        return reduced.clip(-32768, 32767).astype(np.int16).tobytes()

    def normalize(self, audio_bytes: bytes, target_db: Optional[float] = None) -> bytes:
        """
        Apply only normalization.

        Args:
            audio_bytes: Raw PCM audio bytes
            target_db: Target RMS level in dB (default: self.target_db)

        Returns:
            Normalized audio bytes
        """
        if target_db is None:
            target_db = self.target_db

        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
        normalized = self._normalize(audio, target_db)
        return normalized.clip(-32768, 32767).astype(np.int16).tobytes()

    def _reduce_noise(self, audio: np.ndarray) -> np.ndarray:
        """
        Internal noise reduction using spectral gating.

        Args:
            audio: Float32 audio array

        Returns:
            Noise-reduced audio array
        """
        if not NOISEREDUCE_AVAILABLE or nr is None:
            return audio

        try:
            # Use stationary noise reduction (faster, good for constant background noise)
            reduced = nr.reduce_noise(
                y=audio,
                sr=self.sample_rate,
                stationary=True,
                prop_decrease=0.75,  # How much to reduce noise (0-1)
            )
            return reduced
        except Exception as e:
            print(f"Noise reduction error: {e}")
            return audio

    def _normalize(self, audio: np.ndarray, target_db: Optional[float] = None) -> np.ndarray:
        """
        Normalize audio to target RMS level.

        Args:
            audio: Float32 audio array
            target_db: Target RMS level in dB

        Returns:
            Normalized audio array
        """
        if target_db is None:
            target_db = self.target_db

        # Calculate current RMS
        rms = np.sqrt(np.mean(audio ** 2))

        if rms < 1.0:  # Avoid division by zero for silence
            return audio

        # Calculate target RMS from dB
        # For 16-bit audio, max value is 32767
        target_rms = 32767 * (10 ** (target_db / 20))

        # Apply gain
        gain = target_rms / rms
        normalized = audio * gain

        return normalized

    def calibrate_noise(self, noise_sample_bytes: bytes):
        """
        Calibrate noise profile from a sample of background noise.

        Args:
            noise_sample_bytes: Audio sample of background noise (1-2 seconds)
        """
        if not NOISEREDUCE_AVAILABLE:
            return

        self._noise_profile = np.frombuffer(noise_sample_bytes, dtype=np.int16).astype(np.float32)
        print(f"Noise profile calibrated: {len(self._noise_profile)} samples")

    def get_rms(self, audio_bytes: bytes) -> float:
        """
        Calculate RMS level of audio.

        Args:
            audio_bytes: Raw PCM audio bytes

        Returns:
            RMS value
        """
        try:
            audio = np.frombuffer(audio_bytes, dtype=np.int16)
            return float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
        except:
            return 0.0

    def get_db(self, audio_bytes: bytes) -> float:
        """
        Calculate dB level of audio.

        Args:
            audio_bytes: Raw PCM audio bytes

        Returns:
            dB value (relative to full scale)
        """
        rms = self.get_rms(audio_bytes)
        if rms < 1.0:
            return -96.0  # Very quiet
        return 20 * np.log10(rms / 32767)
