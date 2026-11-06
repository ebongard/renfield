"""
Wake word model files served to the satellites.

The backend image bakes `data/wakeword-models/` into MODELS_PATH; satellites
download from `/api/settings/wakeword/models/{id}`. The config push carries a
SHA-256 per model so a satellite that already has an older file of the same
name replaces it — before, a model existed or not, and a retrained model never
reached a satellite that had the previous one.
"""

import hashlib
from pathlib import Path

from loguru import logger

MODELS_PATH = Path("/app/wakeword-models")

# (path, size, mtime_ns) -> sha256 hex; a file baked into the image does not change.
_sha_cache: dict[tuple[str, int, int], str] = {}


def find_model_file(model_id: str) -> Path | None:
    """Find a wake word model file (TFLite or ONNX) for the given model ID."""
    if not MODELS_PATH.exists():
        logger.warning(f"Models path does not exist: {MODELS_PATH}")
        return None

    # Try exact match first (TFLite, then ONNX)
    for ext in [".tflite", ".onnx"]:
        exact_path = MODELS_PATH / f"{model_id}{ext}"
        if exact_path.exists():
            return exact_path

    # Try with version suffix
    for ext in [".tflite", ".onnx"]:
        versioned_path = MODELS_PATH / f"{model_id}_v0.1{ext}"
        if versioned_path.exists():
            return versioned_path

    # Search for any matching file
    for model_file in MODELS_PATH.glob("*"):
        if model_file.suffix not in (".tflite", ".onnx"):
            continue
        # Skip preprocessing models
        if model_file.stem in ["melspectrogram", "embedding_model"]:
            continue
        # Check if model_id is in the filename
        if model_id in model_file.stem.lower():
            return model_file

    return None


def model_sha256(model_id: str) -> str | None:
    """SHA-256 (hex) of the file `find_model_file` serves for this id, or None."""
    path = find_model_file(model_id)
    if path is None:
        return None
    try:
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if key not in _sha_cache:
            _sha_cache[key] = hashlib.sha256(path.read_bytes()).hexdigest()
        return _sha_cache[key]
    except OSError as e:
        logger.warning(f"Cannot hash wake word model {model_id}: {e}")
        return None


def model_checksums(model_ids: list[str]) -> dict[str, str]:
    """`{model_id: sha256}` for every id that has a served file."""
    out = {}
    for model_id in model_ids:
        sha = model_sha256(model_id)
        if sha:
            out[model_id] = sha
    return out
