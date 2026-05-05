"""B.1.0 ECAPA-TDNN ONNX parity tests.

Two gates protect the speaker-recognition migration to voice-server:

1. **CPU parity (this file, runs in CI):** the exported ONNX must produce
   embeddings indistinguishable from in-process speechbrain on CPU FP32.
   Strict tolerance — cosine ≥ 0.999999 per fixture sample. Empirically
   achieved 1.000000 on the validation probe.

2. **GPU quantization parity (build-box only, NOT in this file):** the
   same ONNX run through onnxruntime-gpu with int8_float16 must match
   CPU FP32 ground truth at cosine ≥ 0.99. Lives in the .159 build
   pipeline so Harbor push is blocked if quantization drift exceeds
   tolerance. See VOICE_PIPELINE_DESIGN.md § B.1.0 (review-cycle 3
   GAP-2 fix).

If this file's tests fail, every existing SpeakerEmbedding row in
Postgres becomes un-matchable against voice-server output. Migration
is blocked until cosine drift is understood.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "speaker"
ONNX_PATH = Path(os.environ.get("ECAPA_ONNX_PATH", "/mnt/llm/voice/ecapa_tdnn.onnx"))
COSINE_FLOOR = 0.999999

speechbrain = pytest.importorskip("speechbrain", reason="speechbrain not installed")
ort = pytest.importorskip("onnxruntime", reason="onnxruntime not installed")
torch = pytest.importorskip("torch", reason="torch not installed")
torchaudio = pytest.importorskip("torchaudio", reason="torchaudio not installed")


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    a = a.flatten().astype(np.float64)
    b = b.flatten().astype(np.float64)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def _list_fixtures() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("de_*.wav"))


@pytest.fixture(scope="module")
def encoder():
    from speechbrain.inference.speaker import EncoderClassifier

    return EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        run_opts={"device": "cpu"},
    )


@pytest.fixture(scope="module")
def onnx_session():
    if not ONNX_PATH.exists():
        pytest.skip(f"ONNX missing at {ONNX_PATH} — run scripts/export_ecapa_onnx.py first")
    return ort.InferenceSession(str(ONNX_PATH), providers=["CPUExecutionProvider"])


def _load_wav_resampled(path: Path, target_sr: int = 16000) -> torch.Tensor:
    """Load a WAV and resample to mono 16 kHz, returning shape (1, samples) float32 in [-1, 1]."""
    waveform, sr = torchaudio.load(str(path))
    if waveform.shape[0] > 1:
        waveform = waveform.mean(dim=0, keepdim=True)
    if sr != target_sr:
        waveform = torchaudio.functional.resample(waveform, sr, target_sr)
    return waveform.float()


@pytest.mark.unit
@pytest.mark.parametrize("fixture_path", _list_fixtures(), ids=lambda p: p.name)
def test_ecapa_onnx_parity_per_sample(encoder, onnx_session, fixture_path: Path) -> None:
    """For each fixture WAV: speechbrain in-process vs ONNX CPU FP32 must match cosine ≥ 0.999999."""
    audio = _load_wav_resampled(fixture_path)

    truth = encoder.encode_batch(audio).squeeze().detach().cpu().numpy()

    feats = encoder.mods.compute_features(audio)
    feats_norm = encoder.mods.mean_var_norm(feats, torch.ones(feats.shape[0]))
    onnx_out = onnx_session.run(None, {"features": feats_norm.numpy()})[0]
    candidate = onnx_out.squeeze()

    assert truth.shape == candidate.shape, (
        f"shape mismatch: speechbrain {truth.shape} vs ONNX {candidate.shape}"
    )

    cos = _cosine(truth, candidate)
    assert cos >= COSINE_FLOOR, (
        f"{fixture_path.name}: cosine {cos:.8f} < floor {COSINE_FLOOR} — "
        f"ONNX export drift, voice-server migration blocked"
    )


@pytest.mark.unit
def test_ecapa_fixtures_present() -> None:
    """Sanity: 10 German fixtures exist with non-trivial size."""
    fixtures = _list_fixtures()
    assert len(fixtures) == 10, f"expected 10 fixtures, found {len(fixtures)}"
    for f in fixtures:
        assert f.stat().st_size > 10_000, f"{f.name} suspiciously small ({f.stat().st_size} B)"
