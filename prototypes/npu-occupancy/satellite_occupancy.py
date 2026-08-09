"""Satellite-side occupancy handler — prototype.

Adds a `count_occupants` WS request to the satellite, mirroring `capture_snapshot`
(websocket_client.py:681/746). The backend asks "how many people in your room?"; the
satellite grabs ONE frame from its CSI camera, runs the NPU person-detector locally, and
replies with just an integer. The frame never leaves the device.

Wire-in on the satellite (3 small edits to renfield_satellite/network/websocket_client.py):

  1. register capabilities: add `"has_npu_occupancy": self._occupancy is not None`
     next to `has_camera` (satellite_manager reads it to choose this path).

  2. dispatch (near line 681, beside capture_snapshot):
         elif msg_type == "count_occupants":
             asyncio.create_task(self._handle_count_occupants(data.get("request_id")))

  3. handler: `_handle_count_occupants` below (bound as a method).

The detector + camera are injected once at startup (see `build_occupancy()`), so the hot
path never re-loads the NBG.
"""
from __future__ import annotations

import asyncio
from typing import Optional

# In-repo prototype import; on the satellite this ships inside renfield_satellite/vision/.
from occupancy_detector import DetectorConfig, OccupancyDetector


class V4L2Camera:
    """Single-frame grabber for the A733 CSI sensor exposed as /dev/videoN.

    CSI (not a webcam): the sensor driver lives on the HOST; the privileged pod mounts
    /dev/video* + /dev/media*. One frame per request — no continuous streaming (occupancy
    checks are occasional; a persistent stream adds heat on the passively-hot A733).

    IMPORTANT — sunxi-vin does NOT capture via plain V4L2 / OpenCV.
    ---------------------------------------------------------------
    Verified on the Esszimmer board (2026-08-09): the sensor is detected and /dev/video0
    exists, but `cv2.VideoCapture` and raw `v4l2-ctl --stream-mmap` BOTH fail — the device
    is a media-controller, MULTIPLANAR node, and its ISP/scaler pipeline
    (sensor → mipi → csi → tdm → isp → scaler → vin_cap) will not negotiate a format from
    userspace V4L2 alone (`vin_pipeline_try_format failed`, `scaler get_selection error`).
    The ONLY working capture path is Allwinner's **patched GStreamer `v4l2src`** with the
    `en-awisp=1` property, which drives the AW ISP internally (this is exactly what the
    board's own /usr/local/bin/test_camera.sh uses):

        gst-launch-1.0 v4l2src device=/dev/video0 en-awisp=1 en-largemode=0 num-buffers=1 \
            ! video/x-raw,format=NV12,width=640,height=480 ! jpegenc ! filesink location=...

    That `en-awisp` v4l2src is an Allwinner patch — it is NOT in stock GStreamer and NOT
    apt-installable; it ships only in Allwinner's BSP GStreamer (Orange Pi desktop image).
    So enabling capture is a DEPENDENCY task: get the AW GStreamer v4l2 plugin into the
    satellite image (extract from the Orange Pi desktop rootfs for this board, or build
    from the A733 BSP GStreamer source). See docs/design/a733-satellite-camera.md.
    """

    # AW GStreamer capture → one NV12 frame → JPEG on stdout, decoded to BGR.
    _GST = (
        "gst-launch-1.0 -q v4l2src device={dev} en-awisp=1 en-largemode=0 num-buffers=1 "
        "! video/x-raw,format=NV12,width={w},height={h} ! jpegenc ! fdsink fd=1"
    )

    def __init__(self, device: str = "/dev/video0", width: int = 640, height: int = 480) -> None:
        self._device, self._w, self._h = device, width, height

    async def grab_bgr(self):
        return await asyncio.to_thread(self._grab_sync)

    def _grab_sync(self):
        """Capture one frame via the AW GStreamer pipeline → decode to BGR.

        Returns None if gst / the en-awisp plugin is absent (the current satellite image),
        so the occupancy gate degrades gracefully until the AW plugin ships. Requires
        gst-launch-1.0 WITH Allwinner's en-awisp v4l2src on PATH inside the pod."""
        import shutil, subprocess
        import numpy as np
        from PIL import Image
        import io as _io
        if shutil.which("gst-launch-1.0") is None:
            print("[camera] gst-launch-1.0 (with AW en-awisp v4l2src) not present — capture disabled")
            return None
        cmd = self._GST.format(dev=self._device, w=self._w, h=self._h)
        try:
            out = subprocess.run(cmd.split(), capture_output=True, timeout=15).stdout
            if not out:
                return None
            rgb = np.asarray(Image.open(_io.BytesIO(out)).convert("RGB"))
            return rgb[:, :, ::-1].copy()  # RGB→BGR
        except Exception as e:  # noqa: BLE001
            print(f"[camera] capture failed: {e}")
            return None


class OccupancyProbe:
    """Camera + detector, held for the process lifetime. `count()` = grab → detect."""

    def __init__(self, camera: V4L2Camera, detector: OccupancyDetector) -> None:
        self._camera, self._detector = camera, detector

    async def count(self) -> int | None:
        bgr = await self._camera.grab_bgr()
        if bgr is None:
            return None
        # Detection is CPU/NPU-bound → off the event loop too.
        return await asyncio.to_thread(self._detector.count, bgr)

    def close(self) -> None:
        self._detector.close()


def build_occupancy(model_path: str, device: str = "/dev/video0") -> Optional["OccupancyProbe"]:
    """Construct the probe, or None if the model/camera isn't present (→ satellite
    advertises has_npu_occupancy=False and the backend transparently uses the LLM path)."""
    try:
        det = OccupancyDetector.from_config(DetectorConfig(model_path=model_path))
        return OccupancyProbe(V4L2Camera(device), det)
    except Exception as e:  # noqa: BLE001
        print(f"[occupancy] disabled (no model/camera): {e}")
        return None


# ── The WS handler (bind as WebSocketClient._handle_count_occupants) ───────────
async def _handle_count_occupants(self, request_id):
    """Count people in the room NOW and reply with `occupant_count_result`. ALWAYS
    replies (count=None on no-camera/error) so the backend future never hangs — exactly
    like _handle_capture_snapshot always sending a snapshot_result."""
    count = None
    try:
        if getattr(self, "_occupancy", None) is not None:
            count = await self._occupancy.count()
    except Exception as e:  # noqa: BLE001
        print(f"Occupancy count failed: {e}")
    try:
        await self._send({
            "type": "occupant_count_result", "request_id": request_id, "count": count,
        })
    except Exception as e:  # noqa: BLE001
        print(f"Failed to send occupant_count_result: {e}")
