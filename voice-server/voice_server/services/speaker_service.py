"""ECAPA-TDNN speaker embedding service (D4).

The voice-server runs the ONNX-exported `embedding_model` only — the
full encode_batch pipeline can't be ONNX-traced (PyTorch STFT doesn't
support complex types in opset 17/20). compute_features and
mean_var_norm run in Python via speechbrain (~5 ms CPU per utterance,
negligible vs the ~50 ms GPU embedding inference).

Empirically validated 2026-05-05 with cosine 1.000000 vs in-process
speechbrain across 10 German fixtures (test_speaker_service.py).

Output: 192-dim float32 embedding, ready for backend cosine match.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import numpy as np

from voice_server.config import settings

logger = logging.getLogger(__name__)


class SpeakerService:
    def __init__(self) -> None:
        self._session = None
        self._encoder = None  # speechbrain mods (compute_features, mean_var_norm)
        self.ready: bool = False

    async def warmup(self) -> None:
        path = Path(settings.speaker_model_path)
        if not path.exists():
            logger.error("ECAPA ONNX missing: %s", path)
            return

        logger.info("loading ECAPA ONNX from %s", path)
        loop = asyncio.get_running_loop()

        def _load():
            import onnxruntime as ort
            from speechbrain.inference.speaker import EncoderClassifier

            session = ort.InferenceSession(str(path), providers=settings.speaker_providers)
            # Encoder loaded for compute_features + mean_var_norm only (CPU).
            enc = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                run_opts={"device": "cpu"},
            )
            return session, enc

        self._session, self._encoder = await loop.run_in_executor(None, _load)

        # Warm one inference
        warm = np.zeros(32000, dtype=np.float32)
        await loop.run_in_executor(None, self._embed_sync, warm)
        self.ready = True
        logger.info("speaker service warm (providers=%s)", self._session.get_providers())

    def _embed_sync(self, audio_pcm: np.ndarray) -> np.ndarray:
        """Synchronous embedding extraction. Audio: float32 mono 16 kHz."""
        import torch

        if self._session is None or self._encoder is None:
            raise RuntimeError("SpeakerService not ready")
        if audio_pcm.size == 0:
            raise ValueError("empty audio")

        wave = torch.from_numpy(audio_pcm.astype(np.float32)).unsqueeze(0)
        feats = self._encoder.mods.compute_features(wave)
        feats_norm = self._encoder.mods.mean_var_norm(feats, torch.ones(feats.shape[0]))
        out = self._session.run(None, {"features": feats_norm.numpy()})[0]
        return np.asarray(out, dtype=np.float32).squeeze()

    async def embed(self, audio_pcm: np.ndarray) -> np.ndarray:
        if not self.ready:
            raise RuntimeError("SpeakerService not ready")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._embed_sync, audio_pcm)
