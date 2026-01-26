"""
Wake Word Detector for Renfield Satellite

Supports multiple wake word frameworks:
- pymicro-wakeword (TFLite, lightweight, recommended)
- pyopen-wakeword (TFLite, more models available)
- openwakeword (ONNX, legacy)

Inspired by OHF-Voice/linux-voice-assistant approach.
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
import numpy as np

# Try to import wake word frameworks (prefer TFLite versions)
MICRO_WAKEWORD_AVAILABLE = False
OPEN_WAKEWORD_AVAILABLE = False
ONNX_WAKEWORD_AVAILABLE = False

try:
    from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures, Model as MicroModel
    MICRO_WAKEWORD_AVAILABLE = True
    # Map of built-in model names
    MICRO_BUILTIN_MODELS = {
        "okay_nabu": MicroModel.OKAY_NABU,
        "hey_jarvis": MicroModel.HEY_JARVIS,
        "alexa": MicroModel.ALEXA,
        "hey_mycroft": MicroModel.HEY_MYCROFT,
    }
except ImportError:
    MICRO_BUILTIN_MODELS = {}

try:
    from pyopen_wakeword import OpenWakeWord
    OPEN_WAKEWORD_AVAILABLE = True
except ImportError:
    pass

try:
    from openwakeword.model import Model as OWWModel
    ONNX_WAKEWORD_AVAILABLE = True
except ImportError:
    pass


@dataclass
class WakeWordModel:
    """Information about a loaded wake word model"""
    id: str
    name: str
    path: str
    model_type: str  # "micro", "open", "onnx"
    threshold: float
    model: object  # The actual model instance
    features: object = None  # Feature extractor (for micro models)


@dataclass
class Detection:
    """Wake word detection result"""
    keyword: str
    confidence: float
    timestamp: float
    is_stop_word: bool = False


class WakeWordDetector:
    """
    Wake word detector supporting multiple frameworks.

    Features:
    - Multiple simultaneous wake words
    - Stop word support (cancel ongoing interaction)
    - Refractory period (prevent double triggers)
    - Automatic stereo to mono conversion
    """

    def __init__(
        self,
        models_path: str = "/opt/renfield-satellite/models",
        keywords: Optional[List[str]] = None,
        stop_words: Optional[List[str]] = None,
        threshold: float = 0.5,
        refractory_seconds: float = 2.0,
    ):
        """
        Initialize wake word detector.

        Args:
            models_path: Path to model files
            keywords: List of wake words to detect
            stop_words: List of stop words (cancel commands)
            threshold: Detection threshold 0.0-1.0
            refractory_seconds: Cooldown before re-triggering
        """
        self.models_path = Path(models_path)
        self.keywords = keywords or ["okay_nabu"]
        self.stop_words = stop_words or []
        self.default_threshold = threshold
        self.refractory_seconds = refractory_seconds

        # Loaded models
        self._wake_models: Dict[str, WakeWordModel] = {}
        self._stop_models: Dict[str, WakeWordModel] = {}
        self._loaded = False

        # Refractory tracking
        self._last_detection_time: Dict[str, float] = {}

        # Feature extractors for TFLite models
        self._micro_features = None
        self._open_features = None

        print(f"Wake word frameworks available:")
        print(f"  - pymicro-wakeword: {MICRO_WAKEWORD_AVAILABLE}")
        print(f"  - pyopen-wakeword: {OPEN_WAKEWORD_AVAILABLE}")
        print(f"  - openwakeword (ONNX): {ONNX_WAKEWORD_AVAILABLE}")

    @property
    def available(self) -> bool:
        """Check if any wake word framework is available"""
        return MICRO_WAKEWORD_AVAILABLE or OPEN_WAKEWORD_AVAILABLE or ONNX_WAKEWORD_AVAILABLE

    def _discover_models(self) -> List[dict]:
        """
        Discover available models in models directory.

        Returns:
            List of model info dicts
        """
        models = []
        found_ids = set()  # Track found model IDs to avoid duplicates

        if not self.models_path.exists():
            print(f"Models path does not exist: {self.models_path}")
            return models

        # Look for JSON config files (TFLite models with config)
        for json_file in self.models_path.glob("**/*.json"):
            try:
                with open(json_file) as f:
                    config = json.load(f)

                model_type = config.get("type", "").lower()
                model_path = config.get("model_path", "")

                if not model_path:
                    # Try relative path
                    tflite_file = json_file.with_suffix(".tflite")
                    if tflite_file.exists():
                        model_path = str(tflite_file)

                if model_path and os.path.exists(model_path):
                    model_id = json_file.stem
                    found_ids.add(model_id)
                    models.append({
                        "id": model_id,
                        "name": config.get("name", json_file.stem),
                        "path": model_path,
                        "config_path": str(json_file),
                        "type": "micro" if "micro" in model_type else "open",
                        "threshold": config.get("threshold", self.default_threshold),
                    })
            except Exception as e:
                print(f"Failed to load config {json_file}: {e}")

        # Look for standalone TFLite models (without JSON config)
        for tflite_file in self.models_path.glob("**/*.tflite"):
            # Skip preprocessing models
            if tflite_file.stem in ["melspectrogram", "embedding_model"]:
                continue

            # Skip if already found via JSON config
            model_id = tflite_file.stem.replace("_v0.1", "")
            if model_id in found_ids:
                continue

            found_ids.add(model_id)
            # Use pyopen-wakeword for standalone TFLite files (if available)
            # or fall back to micro-wakeword
            model_type = "open" if OPEN_WAKEWORD_AVAILABLE else "micro"
            models.append({
                "id": model_id,
                "name": tflite_file.stem,
                "path": str(tflite_file),
                "type": model_type,
                "threshold": self.default_threshold,
            })

        # Look for ONNX models (legacy)
        for onnx_file in self.models_path.glob("**/*.onnx"):
            # Skip common models (not wake words)
            if onnx_file.stem in ["melspectrogram", "embedding_model", "silero_vad"]:
                continue

            model_id = onnx_file.stem.replace("_v0.1", "")
            if model_id in found_ids:
                continue

            found_ids.add(model_id)
            models.append({
                "id": model_id,
                "name": onnx_file.stem,
                "path": str(onnx_file),
                "type": "onnx",
                "threshold": self.default_threshold,
            })

        return models

    def load(self) -> bool:
        """
        Load wake word models.

        Returns:
            True if at least one model loaded successfully
        """
        if self._loaded:
            return True

        if not self.available:
            print("No wake word framework available")
            return False

        # Discover available models from files
        available_models = self._discover_models()
        print(f"Found {len(available_models)} wake word models on disk")

        # Load wake word models
        for keyword in self.keywords:
            # First try built-in models (pymicro-wakeword)
            keyword_normalized = keyword.lower().replace("-", "_")
            if MICRO_WAKEWORD_AVAILABLE and keyword_normalized in MICRO_BUILTIN_MODELS:
                model_info = {
                    "id": keyword,
                    "name": keyword,
                    "path": "builtin",
                    "type": "micro",
                    "threshold": self.default_threshold,
                }
                model = self._load_model(model_info)
                if model:
                    self._wake_models[keyword] = model
                    print(f"Loaded wake word: {keyword} (micro/builtin)")
                    continue

            # Otherwise search in discovered models
            model_info = self._find_model(keyword, available_models)
            if model_info:
                model = self._load_model(model_info)
                if model:
                    self._wake_models[keyword] = model
                    print(f"Loaded wake word: {keyword} ({model.model_type})")
            else:
                print(f"No model found for wake word: {keyword}")

        # Load stop word models
        for stop_word in self.stop_words:
            # First try built-in models
            stop_normalized = stop_word.lower().replace("-", "_")
            if MICRO_WAKEWORD_AVAILABLE and stop_normalized in MICRO_BUILTIN_MODELS:
                model_info = {
                    "id": stop_word,
                    "name": stop_word,
                    "path": "builtin",
                    "type": "micro",
                    "threshold": self.default_threshold,
                }
                model = self._load_model(model_info)
                if model:
                    self._stop_models[stop_word] = model
                    print(f"Loaded stop word: {stop_word} (micro/builtin)")
                    continue

            # Otherwise search in discovered models
            model_info = self._find_model(stop_word, available_models)
            if model_info:
                model = self._load_model(model_info)
                if model:
                    self._stop_models[stop_word] = model
                    print(f"Loaded stop word: {stop_word} ({model.model_type})")
            else:
                print(f"No model found for stop word: {stop_word}")

        self._loaded = len(self._wake_models) > 0
        return self._loaded

    def _find_model(self, keyword: str, available_models: List[dict]) -> Optional[dict]:
        """Find a model matching the keyword"""
        keyword_lower = keyword.lower().replace("_", "").replace("-", "")

        for model in available_models:
            model_id = model["id"].lower().replace("_", "").replace("-", "")
            if keyword_lower in model_id or model_id in keyword_lower:
                return model

        print(f"No model found for: {keyword}")
        return None

    def _load_model(self, model_info: dict) -> Optional[WakeWordModel]:
        """Load a single model"""
        model_type = model_info["type"]
        model_path = model_info["path"]
        model_id = model_info["id"].lower().replace("-", "_")

        try:
            if model_type == "micro" and MICRO_WAKEWORD_AVAILABLE:
                # Check if it's a built-in model
                if model_id in MICRO_BUILTIN_MODELS:
                    model = MicroWakeWord.from_builtin(MICRO_BUILTIN_MODELS[model_id])
                else:
                    # Load from file
                    model = MicroWakeWord(model_path)

                # Create feature extractor for this model
                features = MicroWakeWordFeatures()

                return WakeWordModel(
                    id=model_info["id"],
                    name=model_info["name"],
                    path=model_path,
                    model_type="micro",
                    threshold=model_info.get("threshold", self.default_threshold),
                    model=model,
                    features=features,
                )

            elif model_type == "open" and OPEN_WAKEWORD_AVAILABLE:
                config_path = model_info.get("config_path")
                if config_path:
                    model = OpenWakeWord.from_config(config_path)
                else:
                    model = OpenWakeWord.from_file(model_path)
                return WakeWordModel(
                    id=model_info["id"],
                    name=model_info["name"],
                    path=model_path,
                    model_type="open",
                    threshold=model_info.get("threshold", self.default_threshold),
                    model=model,
                )

            elif model_type == "onnx" and ONNX_WAKEWORD_AVAILABLE:
                # Load ONNX wake word model
                # Only pass the actual wake word model file, not preprocessing models
                # openwakeword will find melspectrogram.onnx and embedding_model.onnx automatically
                model = OWWModel(
                    wakeword_models=[model_path],
                    inference_framework="onnx",
                )
                return WakeWordModel(
                    id=model_info["id"],
                    name=model_info["name"],
                    path=model_path,
                    model_type="onnx",
                    threshold=model_info.get("threshold", self.default_threshold),
                    model=model,
                )

        except Exception as e:
            print(f"Failed to load model {model_info['id']}: {e}")

        return None

    def process_audio(self, audio_bytes: bytes) -> Optional[Detection]:
        """
        Process audio chunk for wake word detection.

        Args:
            audio_bytes: Raw PCM audio (16-bit, 16kHz, mono)

        Returns:
            Detection object if wake/stop word detected, None otherwise
        """
        if not self._loaded:
            return None

        current_time = time.time()

        # Check wake words
        for keyword, model in self._wake_models.items():
            # Check refractory period
            last_time = self._last_detection_time.get(keyword, 0)
            if current_time - last_time < self.refractory_seconds:
                continue

            detected, confidence = self._run_detection(model, audio_bytes)
            if detected:
                self._last_detection_time[keyword] = current_time
                print(f"Wake word detected in wakeword detector: {keyword} ({confidence:.2f})")
                return Detection(
                    keyword=keyword,
                    confidence=confidence,
                    timestamp=current_time,
                    is_stop_word=False,
                )

        # Check stop words
        for stop_word, model in self._stop_models.items():
            detected, confidence = self._run_detection(model, audio_bytes)
            if detected:
                print(f"Stop word detected: {stop_word} ({confidence:.2f})")
                return Detection(
                    keyword=stop_word,
                    confidence=confidence,
                    timestamp=current_time,
                    is_stop_word=True,
                )

        return None

    def _run_detection(self, model: WakeWordModel, audio_bytes: bytes) -> Tuple[bool, float]:
        """
        Run detection on a single model.

        Returns:
            Tuple of (detected: bool, confidence: float)
        """
        try:
            if model.model_type == "micro":
                # pymicro-wakeword uses streaming feature extraction
                # Audio must be 16-bit mono 16kHz, process in 10ms chunks
                for features in model.features.process_streaming(audio_bytes):
                    if model.model.process_streaming(features):
                        return True, 1.0  # micro returns bool, not confidence
                return False, 0.0

            elif model.model_type == "open":
                # pyopen-wakeword - similar streaming API
                audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
                audio_float = audio_int16.astype(np.float32) / 32768.0
                scores = model.model.process_audio(audio_float)
                if scores:
                    max_score = max(scores.values())
                    return max_score >= model.threshold, max_score
                return False, 0.0

            elif model.model_type == "onnx":
                # openwakeword expects int16 audio, returns dict {model_name: score}
                audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
                prediction = model.model.predict(audio_int16)

                # Find the best matching score
                for key, score in prediction.items():
                    # Score is already a float
                    score_val = float(score) if not isinstance(score, float) else score
                    if score_val >= model.threshold:
                        return True, score_val

                # Return max score even if below threshold
                if prediction:
                    max_score = max(prediction.values())
                    return False, float(max_score)
                return False, 0.0

        except Exception as e:
            print(f"Detection error for {model.id}: {e}")
            return False, 0.0

        return False, 0.0

    def reset(self):
        """Reset detector state (call after wake word action completes)"""
        # Reset ONNX models if needed
        for model in list(self._wake_models.values()) + list(self._stop_models.values()):
            if model.model_type == "onnx" and hasattr(model.model, "reset"):
                model.model.reset()

    def set_threshold(self, threshold: float, keyword: Optional[str] = None):
        """
        Update detection threshold.

        Args:
            threshold: New threshold 0.0-1.0
            keyword: Specific keyword to update, or None for all
        """
        threshold = max(0.0, min(1.0, threshold))

        if keyword and keyword in self._wake_models:
            self._wake_models[keyword].threshold = threshold
        elif keyword and keyword in self._stop_models:
            self._stop_models[keyword].threshold = threshold
        else:
            self.default_threshold = threshold
            for model in self._wake_models.values():
                model.threshold = threshold
            for model in self._stop_models.values():
                model.threshold = threshold

    def set_refractory_period(self, seconds: float):
        """Set refractory period"""
        self.refractory_seconds = max(0.0, seconds)

    def set_keywords(self, keywords: List[str]):
        """
        Update the list of keywords to detect.

        Note: This only adds new keywords, doesn't remove existing ones.
        For a full replacement, create a new detector instance.
        Keywords that can't be loaded (no model available) are silently skipped.
        """
        loaded = []
        skipped = []

        for keyword in keywords:
            if keyword in self._wake_models:
                loaded.append(keyword)
                continue

            if self.add_keyword(keyword):
                loaded.append(keyword)
            else:
                skipped.append(keyword)

        if skipped:
            print(f"Skipped unavailable wake words from server config: {skipped}")
        if loaded:
            print(f"Active wake words: {list(self._wake_models.keys())}")

    def add_keyword(self, keyword: str) -> bool:
        """Add a new keyword to detect"""
        if keyword in self._wake_models:
            return True

        # First try built-in models (pymicro-wakeword)
        keyword_normalized = keyword.lower().replace("-", "_")
        if MICRO_WAKEWORD_AVAILABLE and keyword_normalized in MICRO_BUILTIN_MODELS:
            model_info = {
                "id": keyword,
                "name": keyword,
                "path": "builtin",
                "type": "micro",
                "threshold": self.default_threshold,
            }
            model = self._load_model(model_info)
            if model:
                self._wake_models[keyword] = model
                print(f"Loaded wake word: {keyword} (micro/builtin)")
                return True

        # Otherwise search in discovered models (files)
        available_models = self._discover_models()
        model_info = self._find_model(keyword, available_models)
        if model_info:
            model = self._load_model(model_info)
            if model:
                self._wake_models[keyword] = model
                return True
        return False

    def remove_keyword(self, keyword: str):
        """Remove a keyword"""
        if keyword in self._wake_models:
            del self._wake_models[keyword]

    @property
    def is_loaded(self) -> bool:
        """Check if models are loaded"""
        return self._loaded

    @property
    def active_keywords(self) -> List[str]:
        """Get list of active wake words"""
        return list(self._wake_models.keys())

    @property
    def active_stop_words(self) -> List[str]:
        """Get list of active stop words"""
        return list(self._stop_models.keys())

    def update_config(
        self,
        keywords: Optional[List[str]] = None,
        threshold: Optional[float] = None,
        cooldown_ms: Optional[int] = None,
    ) -> bool:
        """
        Update wake word configuration at runtime.

        This method is called when the server pushes new configuration.

        Args:
            keywords: New list of wake words to detect (replaces existing)
            threshold: New detection threshold (0.0 - 1.0)
            cooldown_ms: New cooldown between detections in milliseconds

        Returns:
            True if at least one keyword is active after update
        """
        changed = False

        # Update threshold if provided
        if threshold is not None:
            old_threshold = self.default_threshold
            self.set_threshold(threshold)
            if old_threshold != threshold:
                print(f"Wake word threshold updated: {old_threshold} -> {threshold}")
                changed = True

        # Update cooldown/refractory period if provided
        if cooldown_ms is not None:
            old_refractory = self.refractory_seconds
            new_refractory = cooldown_ms / 1000.0
            self.set_refractory_period(new_refractory)
            if old_refractory != new_refractory:
                print(f"Wake word cooldown updated: {old_refractory}s -> {new_refractory}s")
                changed = True

        # Update keywords if provided
        if keywords is not None:
            old_keywords = set(self._wake_models.keys())
            new_keywords = set(keywords)

            # Find keywords to add and remove
            to_add = new_keywords - old_keywords
            to_remove = old_keywords - new_keywords

            # Remove old keywords
            for keyword in to_remove:
                self.remove_keyword(keyword)
                print(f"Removed wake word: {keyword}")
                changed = True

            # Add new keywords
            for keyword in to_add:
                if self.add_keyword(keyword):
                    print(f"Added wake word: {keyword}")
                    changed = True
                else:
                    print(f"Failed to add wake word (model not found): {keyword}")

            if changed:
                print(f"Active wake words: {list(self._wake_models.keys())}")

        return len(self._wake_models) > 0
