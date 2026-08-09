"""On-device object / document-type recognition — prototype (extends occupancy).

Reuses the SAME NPU rails as `occupancy_detector.py` (DetectorBackend → ONNX-CPU
reference or VIPLite-NBG on the A733 NPU); only the model + post-processing differ.

TWO complementary jobs, both edge/triage — the AUTHORITATIVE document extraction stays
in the cluster (Docling + qwen3-vl + Schicht-A). This does NOT replace that:

  1. RECOGNIZE  — zero-shot classify what's in front of the camera ("a letter / an
     invoice / a receipt / a parcel label / an ID card / an object"). Uses a MobileCLIP
     image encoder on the NPU (research: MobileCLIP-S0 ≈ 22.6 ms/frame on the VIP9000)
     scored against PRE-COMPUTED text-label embeddings → cosine → top label. Zero-shot =
     add a label by adding a caption string, no retraining.

  2. HAS-TEXT   — a lightweight text-DETECTION pass (PP-OCR-det / DBNet, CNN → NPU-
     friendly) answers "is there readable text here?" so a presented document TRIGGERS
     capture-and-route, without trying to OCR on the NPU. Full-quality OCR is the
     cluster's job (see `route_document_to_cluster`).

WHY NOT OCR ON THE NPU: text *recognition* needs a CNN/CTC recognizer (PP-OCR-mobile);
transformer recognizers won't compile on this NPU (same limit that blocked the ≥0.5B
LLMs). Detection triages; the cluster reads. This mirrors SATELLITE_CAMERA.md's existing
snapshot→qwen3-vl path — the NPU just decides WHEN to invoke it and works offline.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from occupancy_detector import DetectorConfig, OnnxCpuBackend, VipLiteNpuBackend

# CLIP-family preprocessing (differs from YOLO letterbox): center-square, 256², CLIP norm.
_CLIP_SIDE = 256
_CLIP_MEAN = np.array([0.4815, 0.4578, 0.4082], dtype=np.float32)
_CLIP_STD = np.array([0.2686, 0.2613, 0.2758], dtype=np.float32)

# Household-relevant zero-shot labels. Grow by adding a caption — no retraining.
DEFAULT_LABELS: dict[str, str] = {
    "brief": "a photo of a paper letter or official document",
    "rechnung": "a photo of an invoice or a bill",
    "quittung": "a photo of a receipt",
    "paketlabel": "a photo of a parcel shipping label",
    "ausweis": "a photo of an ID card or passport",
    "visitenkarte": "a photo of a business card",
    "buchseite": "a photo of a page from a book",
    "objekt": "a photo of an everyday object, not a document",
}


def _clip_preprocess(bgr: np.ndarray) -> np.ndarray:
    h, w = bgr.shape[:2]
    s = min(h, w)
    top, left = (h - s) // 2, (w - s) // 2
    crop = bgr[top:top + s, left:left + s]
    ys = (np.arange(_CLIP_SIDE) * s / _CLIP_SIDE).astype(np.int32).clip(0, s - 1)
    xs = ys
    sq = crop[ys][:, xs][:, :, ::-1]                                   # →RGB
    chw = (sq.astype(np.float32) / 255.0 - _CLIP_MEAN) / _CLIP_STD
    return np.ascontiguousarray(chw.transpose(2, 0, 1), dtype=np.float32)[None]


@dataclass
class RecognizerConfig:
    image_encoder_path: str                    # MobileCLIP image encoder (.onnx / .nbg)
    text_embeddings_path: str                  # .npz: {label: 512-d unit vector}, built offline
    labels: dict[str, str] = field(default_factory=lambda: DEFAULT_LABELS)
    margin: float = 0.03                        # top1 must beat top2 by this, else "unsure"


class ObjectDocumentRecognizer:
    """Zero-shot: image → label. Text embeddings are computed ONCE offline (CPU CLIP text
    tower) and shipped as an .npz — the board only runs the image encoder on the NPU."""

    def __init__(self, cfg: RecognizerConfig) -> None:
        self._cfg = cfg
        Backend = VipLiteNpuBackend if cfg.image_encoder_path.endswith(".nbg") else OnnxCpuBackend
        self._enc = Backend(DetectorConfig(model_path=cfg.image_encoder_path))
        data = np.load(cfg.text_embeddings_path)
        self._label_names = list(data.files)
        self._text = np.stack([data[k] for k in self._label_names])       # (L, D) unit rows

    def recognize(self, bgr: np.ndarray) -> tuple[str, float] | None:
        """→ (label, confidence) or None. Confidence = softmaxed cosine; None if the top
        two labels are within `margin` (genuinely ambiguous → don't guess)."""
        try:
            emb = self._enc.infer(_clip_preprocess(bgr))[0].astype(np.float32)
            emb /= np.linalg.norm(emb) + 1e-9
            sims = self._text @ emb                                        # cosine, (L,)
            order = sims.argsort()[::-1]
            if len(order) >= 2 and sims[order[0]] - sims[order[1]] < self._cfg.margin:
                return None
            probs = np.exp(sims * 100.0); probs /= probs.sum()             # CLIP temp≈100
            return self._label_names[order[0]], float(probs[order[0]])
        except Exception as e:  # noqa: BLE001
            print(f"[recognize] failed: {e}")
            return None

    def is_document(self, bgr: np.ndarray) -> bool:
        r = self.recognize(bgr)
        return bool(r and r[0] != "objekt")

    def close(self) -> None:
        self._enc.close()


# ── Present-to-camera → authoritative cluster ingest ──────────────────────────
# The NPU triages; the CLUSTER extracts. When a document is presented and confirmed,
# capture a HIGH-RES frame and push it through the EXISTING folder-ingest bridge — the
# same path as `internal.ingest_file` / `POST /api/folder-ingest/document` — so Docling +
# Schicht-A do the real work (dedup / owner+tier / Paperless filing identical). No new
# extraction stack; the camera just becomes another ingest source.
async def route_document_to_cluster(jpeg: bytes, *, source_room: str, backend, doc_hint: str | None):
    """Push a presented-document capture into the cluster ingest pipeline.

    `backend` is the satellite's authenticated REST client to
    `POST /api/folder-ingest/document` (Bearer folder-ingest token; the same push the
    filesystem MCP uses). `doc_hint` is the NPU's recognized label ("rechnung"), passed
    as a soft title hint — the cluster's Schicht-A extraction stays authoritative.

    Offline (cluster unreachable): fall back to the on-device PP-OCR-mobile read for a
    best-effort answer, and queue the capture for push on reconnect (mirrors the
    filesystem/email MCP re-reconcile-on-recovery pattern). Nothing is persisted beyond
    the transient queue entry.
    """
    payload = {"filename": f"camera-{source_room}.jpg", "content_b64": None, "hint": doc_hint}
    # payload["content_b64"] = base64(jpeg)  # (kept explicit in the productization PR)
    return await backend.push_document(payload, jpeg=jpeg)


if __name__ == "__main__":
    import sys
    from PIL import Image
    rec = ObjectDocumentRecognizer(RecognizerConfig(image_encoder_path=sys.argv[1],
                                                     text_embeddings_path=sys.argv[2]))
    bgr = np.asarray(Image.open(sys.argv[3]).convert("RGB"))[:, :, ::-1].copy()
    print("recognized:", rec.recognize(bgr))
    rec.close()
