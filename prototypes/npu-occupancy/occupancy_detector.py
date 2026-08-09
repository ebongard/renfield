"""NPU-offloaded occupancy detector — prototype.

Replaces the GPU-box vision-LLM people-count (`OllamaService.count_people_in_image`,
services/ollama_service.py) with an on-device person-DETECTION model that runs on the
Esszimmer satellite's Allwinner A733 Vivante VIP9000 NPU (3 TOPS INT8).

WHY DETECTION, NOT AN LLM: the VIP9000 runs YOLO-class detectors well (~20 ms/frame)
but cannot host a useful LLM (it tops out at ~360M params — see the a733_npu_driver
benchmarks). Person-counting is a detection task, so this is a perfect fit — AND a
privacy win: the frame is never base64'd off the device; only an integer count leaves
the satellite. The public contract stays identical to the LLM path:

        count(frame_or_jpeg) -> int | None      # None = "couldn't tell" (caller decides fail-open/closed)

BACKENDS (same interface, swap by config):
  * OnnxCpuBackend   — runnable reference on any box (onnxruntime). Proves the pipeline
                       end-to-end today, before the NPU port lands. Also the satellite's
                       CPU fallback if the NBG/VIPLite libs are absent.
  * VipLiteNpuBackend — the deploy target. Loads an ACUITY-exported NBG and runs it via
                       the VIP Lite userspace API (see a733_npu_driver). Not runnable off
                       the board; the pre/post-processing below is shared with the CPU
                       backend so behaviour is identical bar quantization noise.

Model: YOLOv8n (COCO), person = class 0. Nano is enough for "how many people" and keeps
the NBG small; the calibration + INT8 export recipe is in README.md.
"""
from __future__ import annotations

import io
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

# ── Model constants (YOLOv8 COCO export) ──────────────────────────────────────
_INPUT = 640                      # square letterbox side the NBG is compiled for
_PERSON_CLASS = 0                 # COCO class id for "person"
_CONF_THRESH = 0.35               # tuned for indoor occupancy; see README for calibration
_IOU_THRESH = 0.50                # NMS


# ── Shared pre/post-processing (identical for CPU and NPU so counts agree) ─────
def _letterbox(bgr: np.ndarray) -> tuple[np.ndarray, float, tuple[int, int]]:
    """Resize keeping aspect ratio and pad to _INPUT×_INPUT (gray 114). Returns the
    NCHW float32 [0,1] tensor plus the scale + (pad_x, pad_y) needed to map boxes back."""
    h, w = bgr.shape[:2]
    scale = min(_INPUT / h, _INPUT / w)
    nh, nw = int(round(h * scale)), int(round(w * scale))
    # Nearest-neighbour keeps the prototype dependency-light; real deploy uses cv2.resize.
    ys = (np.arange(nh) / scale).astype(np.int32).clip(0, h - 1)
    xs = (np.arange(nw) / scale).astype(np.int32).clip(0, w - 1)
    resized = bgr[ys][:, xs]
    canvas = np.full((_INPUT, _INPUT, 3), 114, dtype=np.uint8)
    pad_y, pad_x = (_INPUT - nh) // 2, (_INPUT - nw) // 2
    canvas[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    rgb = canvas[:, :, ::-1]                                    # BGR→RGB
    chw = np.ascontiguousarray(rgb.transpose(2, 0, 1), dtype=np.float32) / 255.0
    return chw[None], scale, (pad_x, pad_y)


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thresh: float) -> list[int]:
    """Plain NumPy NMS → kept indices (highest score first)."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes.T
    areas = (x2 - x1).clip(0) * (y2 - y1).clip(0)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = (xx2 - xx1).clip(0) * (yy2 - yy1).clip(0)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_thresh]
    return keep


def _count_persons(output: np.ndarray, conf: float = _CONF_THRESH) -> int:
    """Decode a YOLOv8 head → number of `person` boxes surviving conf + NMS.

    `output` is the raw model tensor, shape (1, 84, 8400) or (84, 8400): 4 bbox
    (cx,cy,w,h) + 80 class scores. We only care about class 0, and we only need the
    COUNT — the boxes are in letterbox space but NMS/counting are scale-invariant, so
    no un-letterboxing is required for a head count.
    """
    o = output[0] if output.ndim == 3 else output          # (84, 8400)
    o = o.T                                                  # (8400, 84)
    person_scores = o[:, 4 + _PERSON_CLASS]
    # Reject any box whose person score isn't the dominant class (avoids a poster of a
    # person that the net weakly lights up while a real object dominates).
    dominant = o[:, 4:].argmax(axis=1) == _PERSON_CLASS
    mask = (person_scores >= conf) & dominant
    if not mask.any():
        return 0
    cx, cy, bw, bh = o[mask, 0], o[mask, 1], o[mask, 2], o[mask, 3]
    boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)
    keep = _nms(boxes, person_scores[mask], _IOU_THRESH)
    return len(keep)


def _decode_jpeg(data: bytes) -> np.ndarray:
    """JPEG bytes → HxWx3 BGR uint8. Pillow keeps the prototype dependency-light; the
    on-satellite build already has a JPEG path from the snapshot capture."""
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    return np.asarray(img)[:, :, ::-1].copy()               # RGB→BGR


# ── Backends ──────────────────────────────────────────────────────────────────
@dataclass
class DetectorConfig:
    model_path: str                     # .onnx (CPU) or .nbg (NPU)
    conf_thresh: float = _CONF_THRESH


class DetectorBackend(ABC):
    """Runs the compiled model on one preprocessed NCHW tensor → raw head tensor."""

    @abstractmethod
    def infer(self, nchw: np.ndarray) -> np.ndarray: ...

    @abstractmethod
    def close(self) -> None: ...


class OnnxCpuBackend(DetectorBackend):
    """Runnable reference / satellite CPU fallback. `pip install onnxruntime pillow`."""

    def __init__(self, cfg: DetectorConfig) -> None:
        import onnxruntime as ort
        self._sess = ort.InferenceSession(cfg.model_path, providers=["CPUExecutionProvider"])
        self._in = self._sess.get_inputs()[0].name

    def infer(self, nchw: np.ndarray) -> np.ndarray:
        return self._sess.run(None, {self._in: nchw})[0]

    def close(self) -> None:
        self._sess = None


class VipLiteNpuBackend(DetectorBackend):
    """A733 Vivante VIP9000 deploy target. Loads an ACUITY-exported NBG and runs it via
    the VIP Lite userspace API. Import + calls follow petayyyy/a733_npu_driver's Python
    bindings; this module is only importable ON the board (VIPLite .so present).

    The NBG is INT8-quantized, so the head comes back as int8 + a per-tensor scale; we
    dequantize to float and hand it to the SAME `_count_persons` as the CPU path.
    """

    def __init__(self, cfg: DetectorConfig) -> None:
        import viplite  # from a733_npu_driver userspace libs; board-only
        self._vip = viplite
        self._net = viplite.load_network(cfg.model_path)     # NBG → graph handle
        self._in = viplite.input_tensor(self._net, 0)
        self._out = viplite.output_tensor(self._net, 0)

    def infer(self, nchw: np.ndarray) -> np.ndarray:
        # VIP Lite wants the network's compiled input layout/quant; ACUITY baked the
        # /255 normalization into the graph, so we feed uint8 NCHW here (see README).
        self._vip.set_input(self._in, (nchw * 255.0).astype(np.uint8))
        self._vip.run(self._net)
        raw, scale, zero_point = self._vip.get_output_quant(self._out)
        return (raw.astype(np.float32) - zero_point) * scale

    def close(self) -> None:
        self._vip.release_network(self._net)


# ── Public API ────────────────────────────────────────────────────────────────
class OccupancyDetector:
    """Drop-in replacement for the vision-LLM occupancy count. Same return contract:
    an int people-count, or None when the model is unavailable/errors (caller decides
    fail-open vs fail-closed — the announce gate fails CLOSED on personal messages)."""

    def __init__(self, backend: DetectorBackend, conf_thresh: float = _CONF_THRESH) -> None:
        self._backend = backend
        self._conf = conf_thresh

    @classmethod
    def from_config(cls, cfg: DetectorConfig) -> "OccupancyDetector":
        backend = VipLiteNpuBackend(cfg) if cfg.model_path.endswith(".nbg") else OnnxCpuBackend(cfg)
        return cls(backend, cfg.conf_thresh)

    def count(self, frame: "np.ndarray | bytes") -> int | None:
        """`frame`: BGR uint8 HxWx3, or JPEG bytes (the satellite's snapshot format)."""
        try:
            bgr = _decode_jpeg(frame) if isinstance(frame, (bytes, bytearray)) else frame
            nchw, _, _ = _letterbox(bgr)
            out = self._backend.infer(nchw)
            return _count_persons(out, self._conf)
        except Exception as e:  # noqa: BLE001 — mirror count_people_in_image: never raise into the gate
            print(f"[occupancy] detect failed: {e}")
            return None

    def close(self) -> None:
        self._backend.close()


if __name__ == "__main__":
    # Smoke: python occupancy_detector.py yolov8n.onnx test.jpg
    import sys
    det = OccupancyDetector.from_config(DetectorConfig(model_path=sys.argv[1]))
    with open(sys.argv[2], "rb") as fh:
        print("people:", det.count(fh.read()))
    det.close()
