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

    HOW CAPTURE WORKS ON THE A733 (verified working on the Esszimmer board 2026-08-09).
    ----------------------------------------------------------------------------------
    sunxi-vin will NOT capture via plain V4L2 / OpenCV: the device is a multiplanar
    media-controller whose ISP/scaler pipeline won't produce frames unless Allwinner's ISP
    userspace (libAWIspApi/libisp) is running. So capture goes through
    `renfield_isp_capture` — a small C tool (isp_capture/renfield_isp_capture.c) that starts
    the AW ISP (`CreateAWIspApi → ispStart`), does the sunxi V4L2 mplane grab, and writes one
    frame as raw I420. Confirmed: a real 640×480 frame (Y mean≈113, stddev≈22). The vendor
    `test_camera.sh`'s `en-awisp` GStreamer property was a red herring (exists in no binary).

    Deploy dependency: the satellite image must contain the tool + the AW ISP libs:
        /opt/awisp/renfield_isp_capture
        /opt/awisp/lib/{libAWIspApi.so, libisp.so, libisp_ini.so}   (from the OPi desktop image)
    See docs/design/a733-satellite-camera.md. Degrades to "capture disabled" when absent.
    """

    _CAP = "/opt/awisp/renfield_isp_capture"
    _LIBS = "/opt/awisp/lib"

    def __init__(self, device: str = "/dev/video0", width: int = 640, height: int = 480,
                 warmup: int = 8) -> None:
        self._device, self._w, self._h, self._warmup = device, width, height, warmup

    async def grab_bgr(self):
        return await asyncio.to_thread(self._grab_sync)

    def _grab_sync(self):
        """Capture one frame via renfield_isp_capture → raw I420 → BGR (numpy, no cv2).

        Returns None if the ISP capture tool/libs aren't installed (so the occupancy gate
        degrades gracefully), or on any capture error."""
        import os, subprocess, tempfile
        import numpy as np
        if not os.path.exists(self._CAP):
            print("[camera] renfield_isp_capture not installed — capture disabled")
            return None
        out = tempfile.mktemp(suffix=".i420")
        env = {**os.environ, "LD_LIBRARY_PATH": self._LIBS}
        try:
            subprocess.run([self._CAP, self._device, str(self._w), str(self._h), out,
                            str(self._warmup)], env=env, timeout=20, capture_output=True)
            if not os.path.exists(out) or os.path.getsize(out) < self._w * self._h:
                return None
            buf = np.fromfile(out, dtype=np.uint8)
            return self._i420_to_bgr(buf, self._w, self._h)
        except Exception as e:  # noqa: BLE001
            print(f"[camera] capture failed: {e}")
            return None
        finally:
            try:
                os.unlink(out)
            except OSError:
                pass

    @staticmethod
    def _i420_to_bgr(buf, w: int, h: int):
        """Raw I420 (Y, then U, then V; chroma at w/2×h/2) → BGR uint8 (BT.601)."""
        import numpy as np
        ysz, csz = w * h, (w // 2) * (h // 2)
        Y = buf[:ysz].reshape(h, w).astype(np.float32)
        U = buf[ysz:ysz + csz].reshape(h // 2, w // 2).astype(np.float32) - 128.0
        V = buf[ysz + csz:ysz + 2 * csz].reshape(h // 2, w // 2).astype(np.float32) - 128.0
        U = np.repeat(np.repeat(U, 2, 0), 2, 1)          # upsample chroma to full res
        V = np.repeat(np.repeat(V, 2, 0), 2, 1)
        R = Y + 1.402 * V
        G = Y - 0.344 * U - 0.714 * V
        B = Y + 1.772 * U
        return np.stack([B, G, R], axis=2).clip(0, 255).astype(np.uint8)


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
