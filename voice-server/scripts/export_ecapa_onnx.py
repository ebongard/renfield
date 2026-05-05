"""B.1.0 ECAPA-TDNN ONNX export.

Exports the SpeechBrain ECAPA-TDNN `embedding_model` (only — not the full
encode_batch pipeline) to ONNX, so the voice-server can run speaker
embedding inference via onnxruntime-gpu without bundling speechbrain.

The full-pipeline export is blocked by a known PyTorch ONNX exporter
limitation: STFT does not currently support complex types in opset 17 or
20 — the internal Fbank STFT inside compute_features can't be traced. The
voice-server replicates compute_features + mean_var_norm in Python before
each ONNX call.

Empirically validated 2026-05-05: cosine 1.000000 vs speechbrain ground
truth on a deterministic pseudo-audio fixture (see VOICE_PIPELINE_DESIGN.md
§ B.1.0).

Usage (run inside the renfield-backend pod which has speechbrain):
    pip install onnx                # one-shot, not required at runtime
    python scripts/export_ecapa_onnx.py --output /mnt/llm/voice/ecapa_tdnn.onnx
"""

import argparse
import sys
from pathlib import Path

import torch
from speechbrain.inference.speaker import EncoderClassifier


DEFAULT_OUTPUT = "/mnt/llm/voice/ecapa_tdnn.onnx"
SOURCE_HF = "speechbrain/spkrec-ecapa-voxceleb"


def export(output_path: Path, opset: int = 17) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"loading {SOURCE_HF} on cpu...", flush=True)
    enc = EncoderClassifier.from_hparams(source=SOURCE_HF, run_opts={"device": "cpu"})

    # Dummy input shaped (B, T, 80) — output of compute_features + mean_var_norm
    # for 2 s of 16 kHz mono audio.
    dummy_audio = torch.randn(1, 32000)
    feats = enc.mods.compute_features(dummy_audio)
    feats_norm = enc.mods.mean_var_norm(feats, torch.ones(feats.shape[0]))
    print(f"feature shape: {tuple(feats_norm.shape)}", flush=True)

    print(f"exporting embedding_model → {output_path} (opset={opset})...", flush=True)
    torch.onnx.export(
        enc.mods.embedding_model,
        feats_norm,
        str(output_path),
        input_names=["features"],
        output_names=["embedding"],
        dynamic_axes={"features": {1: "T"}},
        opset_version=opset,
    )

    size_mb = output_path.stat().st_size / 1024 / 1024
    print(f"OK — {size_mb:.1f} MB written to {output_path}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(DEFAULT_OUTPUT),
        help=f"ONNX output path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    export(args.output, opset=args.opset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
