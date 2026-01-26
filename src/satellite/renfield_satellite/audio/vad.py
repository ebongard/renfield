"""
Voice Activity Detection (VAD) for Renfield Satellite

Provides multiple VAD backends:
- RMS-based (simple, always available)
- WebRTC VAD (fast, lightweight)
- Silero VAD (ML-based, most accurate)
"""

import numpy as np
from typing import Optional, Literal
from enum import Enum


class VADBackend(str, Enum):
    """Available VAD backends"""
    RMS = "rms"           # Simple RMS threshold
    WEBRTC = "webrtc"     # Google WebRTC VAD
    SILERO = "silero"     # Silero VAD (ML-based)


# Try to import optional VAD libraries
WEBRTC_AVAILABLE = False
SILERO_TORCH_AVAILABLE = False
SILERO_ONNX_AVAILABLE = False

try:
    import webrtcvad
    WEBRTC_AVAILABLE = True
except ImportError:
    webrtcvad = None

try:
    import torch
    SILERO_TORCH_AVAILABLE = True
except ImportError:
    torch = None

try:
    import onnxruntime
    SILERO_ONNX_AVAILABLE = True
except ImportError:
    onnxruntime = None

# Silero is available if either PyTorch or ONNX Runtime is installed
SILERO_AVAILABLE = SILERO_TORCH_AVAILABLE or SILERO_ONNX_AVAILABLE


class VoiceActivityDetector:
    """
    Voice Activity Detection with multiple backends.

    Determines whether audio contains speech or silence.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        backend: VADBackend = VADBackend.RMS,
        rms_threshold: float = 350.0,
        webrtc_aggressiveness: int = 2,
        silero_threshold: float = 0.5,
    ):
        """
        Initialize VAD.

        Args:
            sample_rate: Audio sample rate in Hz
            backend: Which VAD backend to use
            rms_threshold: Threshold for RMS backend
            webrtc_aggressiveness: WebRTC VAD aggressiveness (0-3)
            silero_threshold: Threshold for Silero VAD (0-1)
        """
        self.sample_rate = sample_rate
        self.backend = backend
        self.rms_threshold = rms_threshold
        self.webrtc_aggressiveness = webrtc_aggressiveness
        self.silero_threshold = silero_threshold

        # Backend-specific state
        self._webrtc_vad = None
        self._silero_model = None
        self._silero_utils = None
        self._silero_onnx = None
        self._use_onnx = False

        # Initialize selected backend
        self._init_backend()

    def _init_backend(self):
        """Initialize the selected VAD backend"""
        if self.backend == VADBackend.WEBRTC:
            if not WEBRTC_AVAILABLE:
                print("WebRTC VAD not available, falling back to RMS")
                self.backend = VADBackend.RMS
            else:
                self._webrtc_vad = webrtcvad.Vad(self.webrtc_aggressiveness)
                print(f"VAD: WebRTC (aggressiveness={self.webrtc_aggressiveness})")

        elif self.backend == VADBackend.SILERO:
            if not SILERO_AVAILABLE:
                print("Silero VAD not available (neither torch nor onnxruntime installed), falling back to RMS")
                self.backend = VADBackend.RMS
            else:
                try:
                    self._load_silero_model()
                    print(f"VAD: Silero (threshold={self.silero_threshold})")
                except Exception as e:
                    print(f"Failed to load Silero VAD: {e}, falling back to RMS")
                    self.backend = VADBackend.RMS

        if self.backend == VADBackend.RMS:
            print(f"VAD: RMS (threshold={self.rms_threshold})")

    def _load_silero_model(self):
        """Load Silero VAD model (prefers ONNX Runtime for lower resource usage)"""
        # Try ONNX Runtime first (more lightweight for embedded systems)
        if SILERO_ONNX_AVAILABLE:
            try:
                self._silero_onnx = SileroVADLite(
                    threshold=self.silero_threshold,
                    sample_rate=self.sample_rate,
                )
                if self._silero_onnx.available:
                    self._use_onnx = True
                    print("  Using ONNX Runtime backend")
                    return
            except Exception as e:
                print(f"  ONNX loading failed: {e}")

        # Fall back to PyTorch
        if SILERO_TORCH_AVAILABLE:
            # Load Silero VAD model from torch hub
            self._silero_model, self._silero_utils = torch.hub.load(
                repo_or_dir='snakers4/silero-vad',
                model='silero_vad',
                force_reload=False,
                onnx=False,  # Use PyTorch model
            )
            # Move to CPU and eval mode
            self._silero_model.eval()
            self._use_onnx = False
            print("  Using PyTorch backend")
            return

        raise RuntimeError("Neither ONNX Runtime nor PyTorch available for Silero VAD")

    def is_speech(self, audio_bytes: bytes) -> bool:
        """
        Detect if audio contains speech.

        Args:
            audio_bytes: Raw PCM audio bytes (16-bit, mono)

        Returns:
            True if speech detected, False if silence
        """
        if self.backend == VADBackend.RMS:
            return self._rms_detect(audio_bytes)
        elif self.backend == VADBackend.WEBRTC:
            return self._webrtc_detect(audio_bytes)
        elif self.backend == VADBackend.SILERO:
            return self._silero_detect(audio_bytes)
        else:
            return self._rms_detect(audio_bytes)

    def _rms_detect(self, audio_bytes: bytes) -> bool:
        """RMS-based speech detection"""
        try:
            audio = np.frombuffer(audio_bytes, dtype=np.int16)
            rms = np.sqrt(np.mean(audio.astype(np.float32) ** 2))
            return rms >= self.rms_threshold
        except (ValueError, TypeError):
            return False

    def _webrtc_detect(self, audio_bytes: bytes) -> bool:
        """WebRTC VAD speech detection"""
        if self._webrtc_vad is None:
            return self._rms_detect(audio_bytes)

        try:
            # WebRTC VAD requires specific frame sizes:
            # 10ms, 20ms, or 30ms at 8000, 16000, 32000, or 48000 Hz
            # For 16kHz: 160 (10ms), 320 (20ms), or 480 (30ms) samples
            frame_size = len(audio_bytes) // 2  # 16-bit = 2 bytes per sample

            # If frame size matches, use directly
            if frame_size in (160, 320, 480):
                return self._webrtc_vad.is_speech(audio_bytes, self.sample_rate)

            # Otherwise, check multiple frames and vote
            valid_sizes = [480, 320, 160]  # Prefer larger frames
            for size in valid_sizes:
                if frame_size >= size:
                    # Take the first complete frame
                    frame = audio_bytes[:size * 2]
                    return self._webrtc_vad.is_speech(frame, self.sample_rate)

            # Frame too small, fall back to RMS
            return self._rms_detect(audio_bytes)

        except Exception as e:
            # Fall back to RMS on error
            return self._rms_detect(audio_bytes)

    def _silero_detect(self, audio_bytes: bytes) -> bool:
        """Silero VAD speech detection"""
        # Use ONNX backend if available
        if self._use_onnx and self._silero_onnx is not None:
            return self._silero_onnx.is_speech(audio_bytes)

        # Use PyTorch backend
        if self._silero_model is None:
            return self._rms_detect(audio_bytes)

        try:
            # Convert to float tensor
            audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
            audio = audio / 32768.0  # Normalize to [-1, 1]

            # Convert to torch tensor
            audio_tensor = torch.from_numpy(audio)

            # Get speech probability
            speech_prob = self._silero_model(audio_tensor, self.sample_rate).item()

            return speech_prob >= self.silero_threshold

        except Exception as e:
            # Fall back to RMS on error
            return self._rms_detect(audio_bytes)

    def get_speech_probability(self, audio_bytes: bytes) -> float:
        """
        Get speech probability (0-1).

        Only available for Silero backend, others return 0 or 1.

        Args:
            audio_bytes: Raw PCM audio bytes

        Returns:
            Speech probability (0-1)
        """
        if self.backend == VADBackend.SILERO:
            # ONNX backend
            if self._use_onnx and self._silero_onnx is not None:
                return self._silero_onnx.get_speech_probability(audio_bytes)

            # PyTorch backend
            if self._silero_model is not None:
                try:
                    audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
                    audio = audio / 32768.0
                    audio_tensor = torch.from_numpy(audio)
                    return self._silero_model(audio_tensor, self.sample_rate).item()
                except Exception:
                    pass

        # For other backends, return binary result
        return 1.0 if self.is_speech(audio_bytes) else 0.0

    def reset(self):
        """Reset VAD state (for backends that maintain state)"""
        if self.backend == VADBackend.SILERO:
            # ONNX backend
            if self._use_onnx and self._silero_onnx is not None:
                self._silero_onnx.reset()

            # PyTorch backend
            if self._silero_model is not None:
                try:
                    self._silero_model.reset_states()
                except Exception:
                    pass

    @staticmethod
    def get_available_backends() -> list:
        """Get list of available VAD backends"""
        backends = [VADBackend.RMS]  # Always available
        if WEBRTC_AVAILABLE:
            backends.append(VADBackend.WEBRTC)
        if SILERO_AVAILABLE:
            backends.append(VADBackend.SILERO)
        return backends


class SileroVADLite:
    """
    Lightweight Silero VAD wrapper using ONNX Runtime.

    This is more suitable for Raspberry Pi as it doesn't require
    the full PyTorch installation.

    Supports both old model format (h/c separate) and new format (combined state).
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        threshold: float = 0.5,
        sample_rate: int = 16000,
    ):
        """
        Initialize Silero VAD with ONNX Runtime.

        Args:
            model_path: Path to silero_vad.onnx model
            threshold: Speech detection threshold (0-1)
            sample_rate: Audio sample rate (must be 8000 or 16000)
        """
        self.threshold = threshold
        self.sample_rate = sample_rate
        self.model_path = model_path
        self._session = None
        self._state = None
        self._use_new_format = True  # Detect based on model inputs

        # Try to load ONNX model
        self._load_model()

    def _load_model(self):
        """Load ONNX model"""
        try:
            import onnxruntime as ort
            import os

            if self.model_path is None:
                # Try default locations
                possible_paths = [
                    "/opt/renfield-satellite/models/silero_vad.onnx",
                    "models/silero_vad.onnx",
                    os.path.expanduser("~/.cache/silero/silero_vad.onnx"),
                ]
                for path in possible_paths:
                    if os.path.exists(path):
                        self.model_path = path
                        break

            if self.model_path is None or not os.path.exists(self.model_path):
                print("Silero VAD ONNX model not found")
                print("Download from: https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx")
                return

            # Create ONNX session
            sess_options = ort.SessionOptions()
            sess_options.intra_op_num_threads = 1
            sess_options.inter_op_num_threads = 1
            sess_options.log_severity_level = 3  # Suppress warnings

            self._session = ort.InferenceSession(
                self.model_path,
                sess_options=sess_options,
                providers=['CPUExecutionProvider']
            )

            # Detect model format based on input names
            input_names = [i.name for i in self._session.get_inputs()]
            self._use_new_format = 'state' in input_names

            # Initialize hidden states
            self._reset_states()
            print(f"Silero VAD (ONNX) loaded from {self.model_path}")

        except ImportError:
            print("ONNX Runtime not available for Silero VAD")
        except Exception as e:
            print(f"Failed to load Silero VAD ONNX: {e}")

    def _reset_states(self):
        """Reset LSTM hidden states"""
        if self._use_new_format:
            # New format: combined state tensor (2, batch, 128)
            self._state = np.zeros((2, 1, 128), dtype=np.float32)
        else:
            # Old format: separate h and c tensors (2, 1, 64) each
            self._h = np.zeros((2, 1, 64), dtype=np.float32)
            self._c = np.zeros((2, 1, 64), dtype=np.float32)

    def is_speech(self, audio_bytes: bytes) -> bool:
        """
        Detect if audio contains speech.

        Args:
            audio_bytes: Raw PCM audio bytes (16-bit, mono)

        Returns:
            True if speech detected
        """
        prob = self.get_speech_probability(audio_bytes)
        return prob >= self.threshold

    def get_speech_probability(self, audio_bytes: bytes) -> float:
        """
        Get speech probability.

        Args:
            audio_bytes: Raw PCM audio bytes

        Returns:
            Speech probability (0-1)
        """
        if self._session is None:
            return 0.5  # Neutral if model not loaded

        try:
            # Convert to float32 array
            audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
            audio = audio / 32768.0  # Normalize to [-1, 1]

            # Reshape for model: (batch, samples)
            audio = audio.reshape(1, -1)

            if self._use_new_format:
                # New model format with combined state
                ort_inputs = {
                    'input': audio,
                    'sr': np.array(self.sample_rate, dtype=np.int64),
                    'state': self._state,
                }
                output, self._state = self._session.run(None, ort_inputs)
            else:
                # Old model format with separate h/c
                ort_inputs = {
                    'input': audio,
                    'sr': np.array([self.sample_rate], dtype=np.int64),
                    'h': self._h,
                    'c': self._c,
                }
                output, self._h, self._c = self._session.run(None, ort_inputs)

            return float(output[0][0])

        except Exception as e:
            # Return neutral on error
            return 0.5

    def reset(self):
        """Reset hidden states"""
        self._reset_states()

    @property
    def available(self) -> bool:
        """Check if model is loaded and ready"""
        return self._session is not None
