"""
Beamforming Module for Renfield Satellite

Implements Delay-and-Sum (DAS) beamforming for the ReSpeaker 2-Mics Pi HAT.
Optimized for Pi Zero 2 W with minimal resource usage.

The ReSpeaker 2-Mics HAT has two microphones spaced 58mm apart.
This allows spatial filtering to enhance speech from the front
while suppressing noise from the sides.

Effective frequency range: 600 Hz - 3000 Hz (ideal for speech)
Expected improvement: 3-6 dB SNR gain for side noise
"""

import numpy as np
from typing import Optional, Tuple
from dataclasses import dataclass


@dataclass
class BeamformerConfig:
    """Configuration for beamformer."""
    enabled: bool = False
    mic_spacing: float = 0.058  # ReSpeaker 2-Mics: 58mm
    steering_angle: float = 0.0  # Degrees, 0 = front
    speed_of_sound: float = 343.0  # m/s at ~20°C


class BeamformerDAS:
    """
    Delay-and-Sum beamformer optimized for ReSpeaker 2-Mics Pi HAT.

    Designed for Pi Zero 2 W with minimal resource usage (~5-7% CPU).

    How it works:
    1. Captures stereo audio (left and right microphone)
    2. Calculates time delay based on steering angle
    3. Aligns signals by shifting one channel
    4. Sums aligned signals (constructive interference from target direction)

    Example:
        >>> bf = BeamformerDAS(mic_spacing=0.058)
        >>> stereo = capture.read_stereo()  # Shape: (2, samples)
        >>> mono = bf.process(stereo)       # Enhanced mono
    """

    def __init__(
        self,
        mic_spacing: float = 0.058,  # ReSpeaker 2-Mics: 58mm
        sample_rate: int = 16000,
        steering_angle: float = 0.0,  # Degrees, 0 = front
        speed_of_sound: float = 343.0
    ):
        """
        Initialize Delay-and-Sum beamformer.

        Args:
            mic_spacing: Distance between microphones in meters (default 58mm)
            sample_rate: Audio sample rate in Hz
            steering_angle: Target direction in degrees (0=front, 90=right, -90=left)
            speed_of_sound: Speed of sound in m/s (default 343 at ~20°C)
        """
        self.mic_spacing = mic_spacing
        self.sample_rate = sample_rate
        self.speed_of_sound = speed_of_sound
        self.steering_angle = steering_angle

        # Pre-calculate delay for fixed steering
        self._delay_samples = self._calculate_delay(steering_angle)

        # Statistics
        self._frames_processed = 0

    def _calculate_delay(self, angle_degrees: float) -> int:
        """
        Calculate integer sample delay for steering angle.

        The delay is based on the path length difference between
        microphones for sound arriving from a given angle.

        Args:
            angle_degrees: Steering angle (0=front, 90=right)

        Returns:
            Delay in samples (positive = delay left channel)
        """
        angle_rad = np.radians(angle_degrees)
        # Path difference = spacing * sin(angle)
        delay_seconds = self.mic_spacing * np.sin(angle_rad) / self.speed_of_sound
        return int(round(delay_seconds * self.sample_rate))

    def set_steering_angle(self, angle_degrees: float) -> None:
        """
        Update steering angle (recomputes delay).

        Args:
            angle_degrees: New steering angle
        """
        self.steering_angle = angle_degrees
        self._delay_samples = self._calculate_delay(angle_degrees)

    def process(self, stereo_audio: np.ndarray) -> np.ndarray:
        """
        Apply delay-and-sum beamforming to stereo audio.

        Args:
            stereo_audio: Shape (2, samples) or (samples, 2)
                         Float32, normalized to [-1, 1]

        Returns:
            Enhanced mono audio, shape (samples,), float32
        """
        # Handle both (2, N) and (N, 2) shapes
        if stereo_audio.ndim == 1:
            # Already mono, return as-is
            return stereo_audio.astype(np.float32)

        if stereo_audio.shape[0] != 2:
            if stereo_audio.shape[1] == 2:
                stereo_audio = stereo_audio.T
            else:
                raise ValueError(f"Expected stereo audio, got shape {stereo_audio.shape}")

        left, right = stereo_audio[0], stereo_audio[1]
        delay = self._delay_samples

        # Apply delay to align signals from target direction
        if delay > 0:
            # Delay left channel (sound arriving from right side first)
            left_aligned = np.concatenate([np.zeros(delay, dtype=left.dtype), left[:-delay]])
            right_aligned = right
        elif delay < 0:
            # Delay right channel (sound arriving from left side first)
            delay = abs(delay)
            left_aligned = left
            right_aligned = np.concatenate([np.zeros(delay, dtype=right.dtype), right[:-delay]])
        else:
            # No delay needed (front-facing, angle=0)
            left_aligned = left
            right_aligned = right

        # Sum aligned signals (constructive interference from target direction)
        # Divide by 2 to maintain amplitude
        enhanced = (left_aligned + right_aligned) * 0.5

        self._frames_processed += 1

        return enhanced.astype(np.float32)

    def process_int16(self, stereo_int16: np.ndarray) -> np.ndarray:
        """
        Process int16 stereo audio directly.

        Args:
            stereo_int16: Shape (2, samples), dtype int16

        Returns:
            Enhanced mono, dtype int16
        """
        # Convert to float
        stereo_float = stereo_int16.astype(np.float32) / 32768.0

        # Process
        mono_float = self.process(stereo_float)

        # Convert back
        return (mono_float * 32767).astype(np.int16)

    def process_bytes(self, stereo_bytes: bytes) -> bytes:
        """
        Process interleaved stereo bytes directly.

        This is the most efficient method for real-time processing
        as it avoids intermediate conversions.

        Args:
            stereo_bytes: Raw bytes from PyAudio (interleaved L,R,L,R,...)
                         16-bit signed integers

        Returns:
            Mono bytes (16-bit signed integers)
        """
        # Convert to numpy array and reshape to [2, samples] without extra copy
        audio = np.frombuffer(stereo_bytes, dtype=np.int16)
        stereo = audio.reshape((-1, 2)).T  # [2, samples] view, no copy

        # Process
        mono = self.process_int16(stereo)

        return mono.tobytes()

    def get_stats(self) -> dict:
        """Get processing statistics."""
        return {
            "frames_processed": self._frames_processed,
            "mic_spacing_mm": self.mic_spacing * 1000,
            "steering_angle": self.steering_angle,
            "delay_samples": self._delay_samples,
            "delay_ms": abs(self._delay_samples) / self.sample_rate * 1000,
        }

    @staticmethod
    def get_effective_frequency_range(mic_spacing: float = 0.058) -> Tuple[float, float]:
        """
        Get the effective frequency range for beamforming.

        Based on microphone spacing:
        - Below f_min: Poor spatial discrimination
        - Above f_max: Spatial aliasing (ambiguous directions)

        Args:
            mic_spacing: Microphone spacing in meters

        Returns:
            (f_min, f_max) in Hz
        """
        speed_of_sound = 343.0
        # Maximum frequency (spatial aliasing limit)
        f_max = speed_of_sound / (2 * mic_spacing)
        # Minimum frequency (effective discrimination)
        f_min = speed_of_sound / (10 * mic_spacing)
        return (f_min, f_max)


class AdaptiveBeamformer(BeamformerDAS):
    """
    DAS beamformer with noise floor adaptation.

    During non-speech (VAD=False), updates noise estimate.
    During speech (VAD=True), applies soft noise suppression.

    Slightly more CPU but better performance in varying noise.
    Still feasible on Pi Zero 2 W.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.noise_floor = 0.01  # Initial noise estimate
        self.alpha = 0.98  # Smoothing factor (higher = slower adaptation)
        self._speech_frames = 0
        self._noise_frames = 0

    def process_adaptive(
        self,
        stereo_audio: np.ndarray,
        is_speech: bool = True
    ) -> np.ndarray:
        """
        Process with VAD-based noise adaptation.

        Args:
            stereo_audio: Stereo audio (2, samples)
            is_speech: VAD result (True=speech detected)

        Returns:
            Enhanced mono audio
        """
        # Apply standard beamforming
        enhanced = self.process(stereo_audio)

        if not is_speech:
            # Update noise floor estimate during silence
            level = np.sqrt(np.mean(enhanced ** 2))
            self.noise_floor = self.alpha * self.noise_floor + (1 - self.alpha) * level
            self._noise_frames += 1
        else:
            # Soft noise suppression during speech
            # Attenuate samples near noise floor
            threshold = self.noise_floor * 2
            mask = np.abs(enhanced) > threshold
            enhanced = np.where(mask, enhanced, enhanced * 0.1)
            self._speech_frames += 1

        return enhanced

    def get_stats(self) -> dict:
        """Get processing statistics."""
        stats = super().get_stats()
        stats.update({
            "noise_floor": self.noise_floor,
            "speech_frames": self._speech_frames,
            "noise_frames": self._noise_frames,
        })
        return stats


def deinterleave_stereo(interleaved: bytes) -> Tuple[np.ndarray, np.ndarray]:
    """
    Deinterleave stereo audio bytes to separate channels.

    Args:
        interleaved: Raw bytes (L0, R0, L1, R1, ...), 16-bit

    Returns:
        (left, right) as int16 numpy arrays
    """
    audio = np.frombuffer(interleaved, dtype=np.int16)
    left = audio[0::2]
    right = audio[1::2]
    return left, right


def interleave_stereo(left: np.ndarray, right: np.ndarray) -> bytes:
    """
    Interleave two mono channels to stereo bytes.

    Args:
        left: Left channel, int16
        right: Right channel, int16

    Returns:
        Interleaved bytes (L0, R0, L1, R1, ...)
    """
    stereo = np.empty(len(left) + len(right), dtype=np.int16)
    stereo[0::2] = left
    stereo[1::2] = right
    return stereo.tobytes()
