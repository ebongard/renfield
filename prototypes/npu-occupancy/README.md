# NPU-offloaded occupancy check — prototype

Run the message-relay **camera occupancy check** on the Esszimmer satellite's **Allwinner
A733 Vivante VIP9000 NPU** (3 TOPS INT8) instead of shipping the frame to the GPU-box
vision LLM.

## Why

Today (`services/ollama_service.py::count_people_in_image`, called from the announce
privacy gate — `ANNOUNCE_CAMERA_OCCUPANCY_CHECK`) the satellite captures a JPEG, base64s
it to the backend, and a vision **LLM** (`qwen3-vl:8b`) counts the people. That:

- **burns the GPU** for a task the edge NPU is purpose-built for;
- **sends a room photo off the device** (transient, but it still leaves the satellite);
- **dies with the cluster** — no GPU, no occupancy check.

Person-counting is a **detection** task, and the VIP9000 runs YOLO-class detectors well
(~20 ms/frame). It cannot host a useful LLM (tops out ~360M params — see the
`a733_npu_driver` benchmarks), so an LLM-on-NPU would be pointless; a **YOLOv8n person
detector** on-NPU is the right shape. Bonus: **only an integer leaves the satellite** —
a privacy upgrade over the status quo.

## The seam (why this is low-risk)

The public contract is already perfect — one function, `int | None`:

```
count_people_in_image(image_b64) -> int | None      # None ⇒ "couldn't tell"
```

We add a parallel, camera-local path with the **same return type**, and the announce gate
prefers it when available, else falls back to the existing LLM path **byte-identically**.
No behaviour change when the NPU/camera is absent.

```
                          ┌─ has_npu_occupancy ─▶ request_occupant_count() ─WS─▶ satellite: grab frame ─▶ NPU YOLOv8n ─▶ int
announce gate ─ room cam ─┤                                                       (frame stays on device)
                          └─ else ──────────────▶ request_snapshot() + count_people_in_image()  (LLM, unchanged)
```

## Scope — four capabilities across TWO inference stacks

This prototype grew past occupancy. The satellite's camera + A733 unlock four things, but
they split across **two different inference stacks** — don't conflate them:

| Capability | Stack | Where it runs | Status |
|---|---|---|---|
| **Occupancy count** (announce gate) | NPU detection (YOLOv8n) | A733 NPU | prototype here; replaces the GPU vision-LLM count |
| **Object / document-type recognition** | NPU classification (MobileCLIP zero-shot) | A733 NPU | prototype here; **complements** the existing `qwen3-vl` path (`docs/SATELLITE_CAMERA.md`) |
| **Hand gestures** ("Head A") | **MediaPipe landmarks → CPU** (not NPU) | A733 **A76 CPU** | design + spikes done: `docs/design/non-verbal-communication.md`; Phase-3a starter here |
| **Facial expression / affect** ("Head B") | **MediaPipe FaceLandmarker → CPU** | A733 **A76 CPU** | deferred behind Head A; advisory read scaffolded here |

Key fact (from the `T-SILICON-PROBE` spike): **MediaPipe runs on the A76 CPU, not the
NPU** — its Delegate is CPU/GPU only. So gestures/expression are CPU (~3-5 fps, enough for
static gestures); the NPU is for detection/classification (occupancy, object/document).
The NPU→MediaPipe conversion rail (ACUITY→NBG, built here for detection) is the deferred
acceleration path for landmarks, *not* a Phase-3a dependency.

**Document reading already exists** via the cluster: `docs/SATELLITE_CAMERA.md`'s
snapshot→`qwen3-vl` path answers *"was steht auf diesem Zettel?"* today. The NPU pieces
here are edge **triage** (recognize + has-text) that *route* a presented document into the
**existing** Docling + Schicht-A cluster pipeline — they do not re-implement OCR.

## Files in this prototype

| File | Role |
|---|---|
| `occupancy_detector.py` | Shared letterbox + YOLOv8 decode + NMS + person-count. `OnnxCpuBackend` (runnable reference / CPU fallback) and `VipLiteNpuBackend` (A733 NPU deploy target). Same `count() -> int \| None`. |
| `object_document_recognizer.py` | Zero-shot object/document-type recognition (MobileCLIP image encoder on NPU vs pre-computed text-label embeddings) + a has-text trigger + `route_document_to_cluster` (push a presented doc into the existing folder-ingest pipeline). Reuses `occupancy_detector`'s backends. |
| `nonverbal_starter.py` | Hand-gesture (Head A) + facial-expression (Head B) starter — **MediaPipe on CPU**, implementing `non-verbal-communication.md` Phase 3a. Fail-closed actuation, advisory affect, nothing persisted, separate PSK-bound gesture WS. |
| `satellite_occupancy.py` | `V4L2Camera` (single-frame CSI grab), `OccupancyProbe`, and the `count_occupants` WS handler mirroring `_handle_capture_snapshot`. |
| `README.md` | This — design, model-conversion recipe, integration diff, deploy. |

## Camera (research result, 2026-08-09 — corrected)

**CSI is a dead end on this board today — use an RTSP IP camera.** There is a
**connector/driver collision** on the Zero 3W (A733), confirmed after a Pi-15-pin camera
failed to boot it:

- The A733 vendor kernel (6.6.98, `sun60iw2`) builds drivers only for `IMX219`, `OV13850`,
  `GC05A2`, `GC030A`.
- The board's CSI is a **24-pin FPC** (Allwinner pinout). Orange Pi's own 24-pin camera
  modules are all **OV5640** → **no driver** in this kernel.
- The driver-supported sensors (IMX219/OV13850) ship only in **wrong-connector** form (Pi
  15/22-pin or RK3399). **No verified 15→24-pin adapter exists — do not improvise one**
  (a Pi 15-pin camera on the 24-pin connector already caused a no-boot).
- Net: **no off-the-shelf CSI camera is both connector- AND driver-matched** for this board.
  CSI would require porting a `sunxi-vin` OV5640 driver + a Zero-3W DT overlay (real kernel
  work) — out of scope.

**Recommended camera: an RTSP/ONVIF IP camera** — e.g. **TP-Link Tapo C210** (~€35) or
**Reolink E1 Pro** (~€40). Frames arrive over RTSP: **no host driver, no DT overlay, no
`/dev/video*` mount into the pod** — just network egress, and it slots into the existing
Frigate pipeline. This is the buy-it-today path; it supersedes both the earlier
Waveshare-IMX219 CSI pick AND the "needs a USB camera" line in `non-verbal-communication.md`.

Implication for the prototype: `satellite_occupancy.py`'s `V4L2Camera` becomes an **RTSP
frame grab** (`cv2.VideoCapture("rtsp://…")`) instead of `/dev/video0` — the detector and
WS contract are unchanged (frame in → count/label out).

## Validate the pipeline TODAY (no NPU, no board)

The CPU backend proves the whole count contract before any hardware work:

```bash
cd prototypes/npu-occupancy
pip install onnxruntime pillow numpy
# export a stock YOLOv8n to ONNX (ultralytics) — one-off on any machine:
python -c "from ultralytics import YOLO; YOLO('yolov8n.pt').export(format='onnx', imgsz=640, opset=12)"
python occupancy_detector.py yolov8n.onnx some_room_photo.jpg      # -> people: N
```

Same code path, same pre/post-processing the NPU backend uses — so once the NBG is built,
counts agree bar quantization noise.

## Build the NBG for the NPU (ACUITY, on your workstation)

The VIP9000 runs an **NBG** (Network Binary Graph), produced offline by the **ACUITY
toolkit** in Docker (per `petayyyy/a733_npu_driver`). The private key never touches the
board; only the NBG + VIPLite libs ship to it.

```
# 1. ONNX (from the validate step above)          yolov8n.onnx
# 2. ACUITY import → quantize INT8 → export NBG    (ACUITY 6.30.22 docker image)
pegasus import onnx      --model yolov8n.onnx --output-model yolov8n.json ...
pegasus quantize         --model yolov8n.json --iterations 100 \
                         --dataset ./calib_list.txt \            # 100–300 indoor room frames
                         --quantizer asymmetric_affine --qtype uint8
pegasus export ovxlib    --model yolov8n.json --output yolov8n_nbg   # → yolov8n.nbg
```

Notes that decide whether the counts are usable:
- **Calibration set = real room frames** from the actual camera/mount (evening light,
  people at the far wall). Generic COCO calibration under-counts in a dim room.
- **Bake `/255` into the graph** at import (`--input-normalization`) so the board feeds
  raw `uint8` NCHW — `VipLiteNpuBackend.infer` assumes this.
- Person-detection quantizes cleanly to INT8 (unlike the ≥0.5B LLMs, which collapsed to
  cosine 0.236). Confirm post-quant with ACUITY's cosine metric ≥ 0.99 vs the ONNX head.

## Wire it into Renfield (integration diff — deferred to the productization PR)

Prototype only; these are the exact edits when we promote it out of `prototypes/`.

**Satellite** — `renfield_satellite/network/websocket_client.py` (3 edits, see the
docstring in `satellite_occupancy.py`): advertise `has_npu_occupancy`, dispatch
`count_occupants`, add `_handle_count_occupants`. Construct the probe once at startup via
`build_occupancy(model_path, device)`.

**Backend** — `ha_glue/services/satellite_manager.py`, mirror `request_snapshot`:

```python
# capabilities: add `has_npu_occupancy: bool = False` to SatelliteCapabilities,
# populate from register (like has_camera at line 220/832).

self._pending_counts: dict[str, asyncio.Future] = {}          # beside _pending_snapshots

async def request_occupant_count(self, satellite_id, timeout=8.0) -> int | None:
    sat = self._get(satellite_id)
    if sat is None or not sat.capabilities.has_npu_occupancy:
        return None
    request_id = uuid4().hex
    fut = self._loop.create_future(); self._pending_counts[request_id] = fut
    try:
        await sat.send({"type": "count_occupants", "request_id": request_id})
        return await asyncio.wait_for(fut, timeout)
    except (asyncio.TimeoutError, Exception):
        return None
    finally:
        self._pending_counts.pop(request_id, None)

def resolve_occupant_count(self, request_id, count):               # WS: occupant_count_result
    fut = self._pending_counts.get(request_id)
    if fut and not fut.done(): fut.set_result(count)
```

**Announce gate** — where it does `request_snapshot()` then `count_people_in_image()`:

```python
sat = mgr.get_camera_satellite_for_room(room_id)
count = None
if sat and sat.capabilities.has_npu_occupancy:
    count = await mgr.request_occupant_count(sat.id)     # NPU, frame stays on device
if count is None:                                        # no NPU, or it declined → LLM path
    img = await mgr.request_snapshot(sat.id) if sat else None
    count = await ollama.count_people_in_image(img) if img else None
# unchanged downstream: None → fail-closed on 'personal' (neutral "message waiting")
```

The `None`-means-uncertain contract is preserved end to end, so the fail-closed privacy
property of the announce gate is untouched.

## Deploy to the Esszimmer pod (`k8s/satellite-esszimmer.yaml`)

The pod is already privileged with `hostPath /dev`; a CSI camera adds only the model +
the sensor nodes (driver is on the HOST — see the camera research for the sensor + its
device-tree overlay):

```yaml
volumeMounts:
  - { name: dev-video, mountPath: /dev/video0 }     # CSI sensor (host driver)
  - { name: dev-media, mountPath: /dev/media0 }
  - { name: nbg,       mountPath: /opt/models/occupancy.nbg, subPath: occupancy.nbg }
# volumes: dev-video/dev-media = hostPath /dev/video0,/dev/media0 ; nbg from a ConfigMap/Secret or the image
```

Bake the VIPLite `.so`s + `occupancy.nbg` into the satellite image (like the wakeword
model), set the model path env, done. `MEETING_ENABLED`-style flag: `NPU_OCCUPANCY_ENABLED`.

## Status / open items

- **Runnable now:** CPU reference path (counts people from a JPEG) — the integration
  contract is provable before hardware.
- **Blocked on hardware:** ACUITY export + on-board VIPLite run (needs the A733 SDK + the
  board) and the CSI camera (research agent running — sensor choice sets `/dev/videoN`,
  resolution, FoV; the detector is camera-agnostic so it doesn't block this code).
- **Not yet wired into prod** — deliberately. This is a `prototypes/` scaffold; the
  integration diff above is the productization PR.
