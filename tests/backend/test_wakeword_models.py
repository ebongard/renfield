"""Wake word model files: lookup and the checksum the config push carries.

Before the checksum a satellite only asked "do I have a file of that name?",
so a retrained model never replaced the previous one on a satellite that
already had it (Fitnessraum + Wohnzimmer ran an older renfield_de for months).
"""

import hashlib

import pytest

from services import wakeword_models
from services.wakeword_config_manager import WakeWordConfig


@pytest.fixture
def models_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(wakeword_models, "MODELS_PATH", tmp_path)
    monkeypatch.setattr(wakeword_models, "_sha_cache", {})
    return tmp_path


@pytest.mark.unit
def test_the_checksum_is_the_sha256_of_the_served_file(models_dir):
    (models_dir / "renfield_de.onnx").write_bytes(b"model v5")

    assert wakeword_models.model_sha256("renfield_de") == hashlib.sha256(b"model v5").hexdigest()


@pytest.mark.unit
def test_a_replaced_file_gets_a_new_checksum(models_dir):
    """The cache keys on size + mtime: a new model in the same path is seen."""
    path = models_dir / "renfield_de.onnx"
    path.write_bytes(b"model v4")
    first = wakeword_models.model_sha256("renfield_de")

    path.write_bytes(b"model v5, longer")

    assert wakeword_models.model_sha256("renfield_de") != first
    assert wakeword_models.model_sha256("renfield_de") == hashlib.sha256(b"model v5, longer").hexdigest()


@pytest.mark.unit
def test_a_missing_model_has_no_checksum(models_dir):
    assert wakeword_models.model_sha256("renfield_de") is None
    assert wakeword_models.model_checksums(["renfield_de"]) == {}


@pytest.mark.unit
def test_a_missing_models_directory_has_no_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(wakeword_models, "MODELS_PATH", tmp_path / "absent")

    assert wakeword_models.model_checksums(["renfield_de"]) == {}


@pytest.mark.unit
def test_the_satellite_config_carries_a_checksum_per_served_keyword(models_dir):
    (models_dir / "renfield_de.onnx").write_bytes(b"de")
    (models_dir / "renfield_en.onnx").write_bytes(b"en")
    config = WakeWordConfig(keyword="renfield_de,renfield_en,renfield_it", threshold=0.5, cooldown_ms=2000)

    shas = config.to_satellite_config()["model_sha256"]

    assert shas == {
        "renfield_de": hashlib.sha256(b"de").hexdigest(),
        "renfield_en": hashlib.sha256(b"en").hexdigest(),
    }  # renfield_it has no file here, so no checksum (the satellite falls back to "exists?")


@pytest.mark.unit
def test_the_fuzzy_lookup_never_serves_a_preprocessing_model(models_dir):
    """A partial id must not match the shared feature models."""
    (models_dir / "melspectrogram.onnx").write_bytes(b"mel")
    (models_dir / "embedding_model.onnx").write_bytes(b"emb")

    assert wakeword_models.find_model_file("spectrogram") is None
    assert wakeword_models.find_model_file("embedding") is None
