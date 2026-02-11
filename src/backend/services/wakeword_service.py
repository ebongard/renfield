"""
Wake Word Detection Service (Server-Side Fallback)

This service provides server-side wake word detection using OpenWakeWord
for clients where browser-based WASM detection is not performant.

Audio is streamed via WebSocket in 80ms chunks (1280 samples at 16kHz).
"""


import numpy as np
from loguru import logger

from utils.config import settings

# OpenWakeWord is optional - only loaded if needed
try:
    from openwakeword.model import Model
    OPENWAKEWORD_AVAILABLE = True
except ImportError:
    OPENWAKEWORD_AVAILABLE = False
    logger.warning("OpenWakeWord not installed. Server-side wake word detection unavailable.")
    logger.warning("Install with: pip install openwakeword")


class WakeWordService:
    """
    Server-side wake word detection service using OpenWakeWord.

    This is a fallback for clients where browser-based WASM detection
    is not available or performant (e.g., older mobile devices).
    """

    # Available wake word models
    AVAILABLE_KEYWORDS = [
        "hey_renfield",
        "hey_jarvis",
        "alexa",
        "hey_mycroft",
        "hey_rhasspy",
        "timer",
        "weather",
    ]

    def __init__(self):
        self.model: Model | None = None
        self.keywords: list[str] = [settings.wake_word_default]
        self.threshold: float = settings.wake_word_threshold
        self.cooldown_ms: int = settings.wake_word_cooldown_ms
        self._last_detection_time: dict[str, float] = {}
        self._loaded = False

    @property
    def available(self) -> bool:
        """Check if OpenWakeWord is available."""
        return OPENWAKEWORD_AVAILABLE

    def load_model(self, keywords: list[str] | None = None) -> bool:
        """
        Load the wake word detection model.

        Args:
            keywords: List of wake words to detect. Defaults to configured default.

        Returns:
            True if model loaded successfully, False otherwise.
        """
        if not OPENWAKEWORD_AVAILABLE:
            logger.error("Cannot load model: OpenWakeWord not installed")
            return False

        if self._loaded and self.model is not None:
            logger.debug("Model already loaded")
            return True

        try:
            if keywords:
                self.keywords = [k for k in keywords if k in self.AVAILABLE_KEYWORDS]
                if not self.keywords:
                    logger.warning(f"No valid keywords in {keywords}, using default")
                    self.keywords = [settings.wake_word_default]

            logger.info(f"Loading OpenWakeWord model with keywords: {self.keywords}")

            # Load the model with specified wake words
            self.model = Model(
                wakeword_models=self.keywords,
                inference_framework="onnx"  # Use ONNX for consistency with browser
            )

            self._loaded = True
            logger.info("OpenWakeWord model loaded successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to load OpenWakeWord model: {e}")
            self._loaded = False
            return False

    def process_audio_chunk(self, audio_bytes: bytes) -> dict:
        """
        Process an audio chunk and check for wake word detection.

        Args:
            audio_bytes: Raw audio bytes (16-bit PCM, 16kHz, mono)
                        Expected size: 2560 bytes (1280 samples * 2 bytes/sample)

        Returns:
            Dict with detection result:
            {
                "detected": bool,
                "keyword": str | None,
                "score": float | None
            }
        """
        if not self._loaded or self.model is None:
            return {"detected": False, "error": "Model not loaded"}

        try:
            # Convert bytes to numpy array (16-bit PCM)
            audio = np.frombuffer(audio_bytes, dtype=np.int16)

            # Normalize to float32 in range [-1, 1]
            audio_float = audio.astype(np.float32) / 32768.0

            # Run prediction
            prediction = self.model.predict(audio_float)

            # Check each keyword for detection
            import time
            current_time = time.time() * 1000  # ms

            for keyword in self.keywords:
                score = prediction.get(keyword, 0.0)

                if score > self.threshold:
                    # Check cooldown
                    last_detection = self._last_detection_time.get(keyword, 0)
                    if current_time - last_detection < self.cooldown_ms:
                        continue  # Still in cooldown

                    # Detection!
                    self._last_detection_time[keyword] = current_time
                    logger.info(f"Wake word detected: {keyword} (score: {score:.3f})")

                    return {
                        "detected": True,
                        "keyword": keyword,
                        "score": float(score)
                    }

            return {"detected": False}

        except Exception as e:
            logger.error(f"Error processing audio chunk: {e}")
            return {"detected": False, "error": str(e)}

    def set_keywords(self, keywords: list[str]) -> bool:
        """
        Change the active wake words.

        Args:
            keywords: List of wake word identifiers

        Returns:
            True if successful
        """
        valid_keywords = [k for k in keywords if k in self.AVAILABLE_KEYWORDS]
        if not valid_keywords:
            logger.warning(f"No valid keywords in {keywords}")
            return False

        self.keywords = valid_keywords

        # Reload model with new keywords
        self._loaded = False
        return self.load_model(valid_keywords)

    def set_threshold(self, threshold: float) -> None:
        """Set the detection threshold (0.0 - 1.0)."""
        self.threshold = max(0.0, min(1.0, threshold))
        logger.debug(f"Wake word threshold set to {self.threshold}")

    def set_cooldown(self, cooldown_ms: int) -> None:
        """Set the cooldown period between detections."""
        self.cooldown_ms = max(0, cooldown_ms)
        logger.debug(f"Wake word cooldown set to {self.cooldown_ms}ms")

    def reset(self) -> None:
        """Reset detection state (clear cooldowns)."""
        self._last_detection_time.clear()

    def get_status(self) -> dict:
        """Get service status information."""
        return {
            "available": self.available,
            "loaded": self._loaded,
            "keywords": self.keywords,
            "threshold": self.threshold,
            "cooldown_ms": self.cooldown_ms,
            "available_keywords": self.AVAILABLE_KEYWORDS,
        }


# Singleton instance
_wakeword_service: WakeWordService | None = None


def get_wakeword_service() -> WakeWordService:
    """Get or create the wake word service singleton."""
    global _wakeword_service
    if _wakeword_service is None:
        _wakeword_service = WakeWordService()
    return _wakeword_service
