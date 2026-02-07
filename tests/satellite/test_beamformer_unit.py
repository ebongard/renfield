"""
Beamformer Unit Tests

Tests for renfield_satellite.audio.beamformer.BeamformerDAS:
- process() with known stereo input shape (2, N) -> mono output shape (N,)
- process_bytes() round-trip: stereo bytes -> mono bytes with correct sample count
- Zero steering angle (default) = simple averaging of channels
- get_stats() returns dict with expected keys
- get_effective_frequency_range() returns reasonable values for 58mm spacing
"""

import numpy as np
import pytest

from renfield_satellite.audio.beamformer import BeamformerDAS


class TestBeamformerProcess:
    """Tests for BeamformerDAS.process() method."""

    @pytest.mark.satellite
    def test_process_stereo_2xN_returns_mono_N(self):
        """process() with shape (2, N) input returns shape (N,) output."""
        bf = BeamformerDAS(mic_spacing=0.058, sample_rate=16000, steering_angle=0.0)
        num_samples = 1600
        stereo = np.random.randn(2, num_samples).astype(np.float32)

        mono = bf.process(stereo)

        assert mono.ndim == 1
        assert mono.shape == (num_samples,)
        assert mono.dtype == np.float32

    @pytest.mark.satellite
    def test_process_stereo_Nx2_returns_mono_N(self):
        """process() with shape (N, 2) input is transposed and returns shape (N,) output."""
        bf = BeamformerDAS(mic_spacing=0.058, sample_rate=16000, steering_angle=0.0)
        num_samples = 1600
        stereo = np.random.randn(num_samples, 2).astype(np.float32)

        mono = bf.process(stereo)

        assert mono.ndim == 1
        assert mono.shape == (num_samples,)

    @pytest.mark.satellite
    def test_process_mono_input_passthrough(self):
        """process() with 1D (mono) input returns the same array as float32."""
        bf = BeamformerDAS()
        mono_input = np.array([0.1, 0.2, 0.3], dtype=np.float32)

        result = bf.process(mono_input)

        assert result.ndim == 1
        assert result.dtype == np.float32
        np.testing.assert_array_almost_equal(result, mono_input)

    @pytest.mark.satellite
    def test_process_invalid_shape_raises_error(self):
        """process() with invalid shape (e.g., (3, N)) raises ValueError."""
        bf = BeamformerDAS()
        invalid = np.random.randn(3, 100).astype(np.float32)

        with pytest.raises(ValueError, match="Expected stereo audio"):
            bf.process(invalid)


class TestBeamformerZeroSteering:
    """Tests that zero steering angle produces simple channel averaging."""

    @pytest.mark.satellite
    def test_zero_steering_averages_channels(self):
        """With steering_angle=0, process() returns the average of both channels."""
        bf = BeamformerDAS(steering_angle=0.0)

        left = np.array([1000.0, 2000.0, 3000.0], dtype=np.float32)
        right = np.array([500.0, 1000.0, 1500.0], dtype=np.float32)
        stereo = np.stack([left, right])  # Shape (2, 3)

        mono = bf.process(stereo)

        expected = (left + right) * 0.5
        np.testing.assert_array_almost_equal(mono, expected)

    @pytest.mark.satellite
    def test_zero_steering_delay_is_zero(self):
        """With steering_angle=0, the internal delay is 0 samples."""
        bf = BeamformerDAS(steering_angle=0.0)
        assert bf._delay_samples == 0

    @pytest.mark.satellite
    def test_nonzero_steering_has_nonzero_delay(self):
        """With a large steering angle, the delay is nonzero."""
        bf = BeamformerDAS(steering_angle=45.0, mic_spacing=0.058, sample_rate=16000)
        # For 45 degrees at 58mm spacing and 16kHz:
        # delay_seconds = 0.058 * sin(45) / 343 ~ 0.0001197s
        # delay_samples = round(0.0001197 * 16000) ~ 2
        assert bf._delay_samples != 0


class TestBeamformerProcessBytes:
    """Tests for BeamformerDAS.process_bytes() method."""

    @pytest.mark.satellite
    def test_process_bytes_stereo_to_mono_sample_count(self):
        """process_bytes() converts stereo bytes to mono bytes with same sample count."""
        bf = BeamformerDAS(steering_angle=0.0)
        num_samples = 640

        # Create interleaved stereo int16 bytes: L0, R0, L1, R1, ...
        left = np.full(num_samples, 5000, dtype=np.int16)
        right = np.full(num_samples, 3000, dtype=np.int16)
        interleaved = np.empty(num_samples * 2, dtype=np.int16)
        interleaved[0::2] = left
        interleaved[1::2] = right
        stereo_bytes = interleaved.tobytes()

        mono_bytes = bf.process_bytes(stereo_bytes)

        # Stereo: num_samples * 2 channels * 2 bytes = num_samples * 4 bytes
        assert len(stereo_bytes) == num_samples * 4
        # Mono: num_samples * 1 channel * 2 bytes = num_samples * 2 bytes
        assert len(mono_bytes) == num_samples * 2

    @pytest.mark.satellite
    def test_process_bytes_roundtrip_content(self):
        """process_bytes() with zero steering averages left and right channels."""
        bf = BeamformerDAS(steering_angle=0.0)
        num_samples = 100

        left = np.full(num_samples, 10000, dtype=np.int16)
        right = np.full(num_samples, 6000, dtype=np.int16)
        interleaved = np.empty(num_samples * 2, dtype=np.int16)
        interleaved[0::2] = left
        interleaved[1::2] = right
        stereo_bytes = interleaved.tobytes()

        mono_bytes = bf.process_bytes(stereo_bytes)
        mono_array = np.frombuffer(mono_bytes, dtype=np.int16)

        # Average of 10000 and 6000 = 8000, with float conversion rounding
        # Allow small tolerance for float32 conversion roundtrip
        expected_avg = (10000 + 6000) // 2
        assert mono_array.shape == (num_samples,)
        np.testing.assert_allclose(mono_array, expected_avg, atol=2)


class TestBeamformerStats:
    """Tests for BeamformerDAS.get_stats() method."""

    @pytest.mark.satellite
    def test_get_stats_returns_expected_keys(self):
        """get_stats() returns a dict with all expected keys."""
        bf = BeamformerDAS()
        stats = bf.get_stats()

        expected_keys = {
            "frames_processed",
            "mic_spacing_mm",
            "steering_angle",
            "delay_samples",
            "delay_ms",
        }
        assert set(stats.keys()) == expected_keys

    @pytest.mark.satellite
    def test_get_stats_frames_processed_increments(self):
        """frames_processed in stats increments after each process() call."""
        bf = BeamformerDAS()

        assert bf.get_stats()["frames_processed"] == 0

        stereo = np.random.randn(2, 320).astype(np.float32)
        bf.process(stereo)
        assert bf.get_stats()["frames_processed"] == 1

        bf.process(stereo)
        assert bf.get_stats()["frames_processed"] == 2

    @pytest.mark.satellite
    def test_get_stats_mic_spacing_in_mm(self):
        """mic_spacing_mm in stats is the spacing in millimeters."""
        bf = BeamformerDAS(mic_spacing=0.058)
        stats = bf.get_stats()
        assert stats["mic_spacing_mm"] == pytest.approx(58.0)


class TestBeamformerFrequencyRange:
    """Tests for BeamformerDAS.get_effective_frequency_range()."""

    @pytest.mark.satellite
    def test_frequency_range_for_58mm_spacing(self):
        """get_effective_frequency_range() returns reasonable values for 58mm spacing."""
        f_min, f_max = BeamformerDAS.get_effective_frequency_range(mic_spacing=0.058)

        # f_min = 343 / (10 * 0.058) ~ 591 Hz
        # f_max = 343 / (2 * 0.058) ~ 2957 Hz
        assert 500 < f_min < 700, f"f_min={f_min} not in expected range"
        assert 2500 < f_max < 3500, f"f_max={f_max} not in expected range"

    @pytest.mark.satellite
    def test_frequency_range_f_min_less_than_f_max(self):
        """f_min is always less than f_max for any valid mic spacing."""
        f_min, f_max = BeamformerDAS.get_effective_frequency_range(mic_spacing=0.058)
        assert f_min < f_max

    @pytest.mark.satellite
    def test_frequency_range_wider_spacing_lowers_range(self):
        """Wider mic spacing produces lower f_min and f_max."""
        f_min_narrow, f_max_narrow = BeamformerDAS.get_effective_frequency_range(mic_spacing=0.058)
        f_min_wide, f_max_wide = BeamformerDAS.get_effective_frequency_range(mic_spacing=0.120)

        assert f_min_wide < f_min_narrow
        assert f_max_wide < f_max_narrow
