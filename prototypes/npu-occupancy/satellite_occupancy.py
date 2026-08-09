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
    """Single-frame grabber for a CSI sensor exposed as /dev/videoN.

    CSI (not a webcam): the sensor driver lives on the HOST; the privileged pod mounts
    /dev/video* + /dev/media*. We open lazily and read one frame per request — no
    continuous streaming (occupancy checks are occasional, and a persistent stream would
    fight the audio loop for USB/DMA bandwidth and power).
    """

    def __init__(self, device: str = "/dev/video0", width: int = 1280, height: int = 720) -> None:
        self._device, self._w, self._h = device, width, height

    async def grab_bgr(self):
        # Run the blocking V4L2 read off the event loop so it never stalls wakeword/WS.
        return await asyncio.to_thread(self._grab_sync)

    def _grab_sync(self):
        import cv2  # opencv-python-headless on the satellite image
        cap = cv2.VideoCapture(self._device, cv2.CAP_V4L2)
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._h)
            # Drop the first couple of frames — CSI sensors need AE/AWB to settle,
            # else the first frame is black/over-exposed and the count is garbage.
            for _ in range(3):
                ok, frame = cap.read()
            return frame if ok else None
        finally:
            cap.release()


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
