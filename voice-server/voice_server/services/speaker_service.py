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

        # Warm one inference. Use low-amplitude noise rather than zeros:
        # all-zero audio runs through compute_features → mean_var_norm with
        # near-zero variance and can NaN-propagate through the ONNX graph.
        # Some EPs silently retry NaN-producing nodes on CPU, which would
        # let warmup pass without exercising the CUDA code path.
        rng = np.random.default_rng(0)
        warm = (rng.standard_normal(32000) * 0.01).astype(np.float32)
        await loop.run_in_executor(None, self._embed_sync, warm)
        self.ready = True

        # NOTE: get_providers() reports providers REGISTERED with the session
        # at construction, not which one actually executed nodes. There is
        # a known onnxruntime issue where CUDA EP is registered but every
        # node falls back to CPU silently (see issue #25145). The warning
        # below catches the catastrophic case (CUDA not registered at all);
        # silent per-node fallback would still pass this check.
        registered = self._session.get_providers()
        logger.info("speaker service warm — registered providers=%s", registered)
        if (
            "CUDAExecutionProvider" not in registered
            and "CUDAExecutionProvider" in settings.speaker_providers
        ):
            logger.warning(
                "ECAPA CUDA provider not registered — running on %s. "
                "Check Dockerfile (cuDNN >=9.1 + onnxruntime-gpu nuke-and-"
                "reinstall) and GPU passthrough on the node.",
                registered[0] if registered else "no-provider",
            )

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
