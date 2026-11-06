"""
Camera Controller for Renfield Satellite

Captures JPEG snapshots for visual queries + backend occupancy checks — snapshot
taken on a backend `capture_snapshot` request, sent for Vision-LLM processing.

Two capture backends, selected by config (`camera.backend`):
  * "rpicam"    Raspberry Pi + Pi Camera via `rpicam-still` (the OS libcamera stack).
  * "sunxi_isp" Orange Pi A733 (Esszimmer) + MIPI-CSI camera. Plain V4L2/OpenCV can't
                capture on sunxi-vin (the Allwinner ISP must run), so this shells out to
                `/opt/awisp/renfield_isp_capture` (which starts the AW ISP + does the
                mplane grab → raw I420) and converts I420 → JPEG here. The tool + AW ISP
                libs are hostPath-mounted into the pod at /opt/awisp
                (k8s/satellite-esszimmer.yaml). See docs/design/a733-satellite-camera.md.

Both return JPEG bytes, so the caller (`_capture_snapshot_for_request` → the WS
`snapshot_result`) is backend-agnostic.
"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

# A733 sunxi_isp backend: the capture tool + Allwinner ISP libs (hostPath-mounted).
_ISP_BIN = "/opt/awisp/renfield_isp_capture"
_ISP_LIBS = "/opt/awisp/lib"
_ISP_WARMUP = 8  # frames skipped for ISP 3A (auto-exposure/white-balance) to settle


class CameraController:
    """open() -> bool init, close() cleanup, capture() -> JPEG bytes | None. Graceful
    degradation when the camera/tooling is absent (capture returns None)."""

    def __init__(self, resolution: str = "1280x720", quality: int = 85,
                 backend: str = "rpicam"):
        self.resolution = resolution
        self.quality = quality
        self.backend = backend
        self._available = False
        self._tmp_dir: Optional[str] = None

    def open(self) -> bool:
        """Verify the selected backend is usable + set up a scratch dir."""
        if self.backend == "sunxi_isp":
            if not os.path.exists(_ISP_BIN):
                print(f"Camera: sunxi_isp backend but {_ISP_BIN} not found "
                      "(is /opt/awisp mounted? see a733-satellite-camera.md)")
                return False
        elif shutil.which("rpicam-still") is None:
            print("Camera: rpicam-still not found")
            return False

        self._tmp_dir = tempfile.mkdtemp(prefix="renfield-cam-")
        self._available = True
        print(f"Camera initialized (backend={self.backend}, resolution={self.resolution}, "
              f"quality={self.quality})")
        return True

    def close(self):
        if self._tmp_dir:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    async def capture(self) -> Optional[bytes]:
        """Capture a JPEG snapshot (non-blocking). Returns JPEG bytes or None."""
        if not self._available or not self._tmp_dir:
            return None
        if self.backend == "sunxi_isp":
            return await self._capture_isp()
        return await self._capture_rpicam()

    # ── rpicam (Raspberry Pi) ─────────────────────────────────────────────────
    async def _capture_rpicam(self) -> Optional[bytes]:
        output_path = str(Path(self._tmp_dir) / "snapshot.jpg")
        width, height = self.resolution.split("x")
        cmd = [
            "rpicam-still", "--immediate", "--nopreview",
            "--width", width, "--height", height,
            "--quality", str(self.quality), "-o", output_path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip() if stderr else "unknown error"
                print(f"Camera capture failed (rc={proc.returncode}): {err}")
                return None
            path = Path(output_path)
            if not path.exists():
                print("Camera capture failed: output file not created")
                return None
            jpeg_bytes = path.read_bytes()
            print(f"Camera captured {len(jpeg_bytes)} bytes (rpicam)")
            return jpeg_bytes
        except asyncio.TimeoutError:
            print("Camera capture timed out (10s)")
            return None
        except Exception as e:  # noqa: BLE001
            print(f"Camera capture error: {e}")
            return None

    # ── sunxi_isp (Orange Pi A733) ────────────────────────────────────────────
    async def _capture_isp(self) -> Optional[bytes]:
        raw = str(Path(self._tmp_dir) / "frame.i420")
        width, height = self.resolution.split("x")
        env = {**os.environ, "LD_LIBRARY_PATH": _ISP_LIBS}
        cmd = [_ISP_BIN, "/dev/video0", width, height, raw, str(_ISP_WARMUP)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, env=env, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
            path = Path(raw)
            if proc.returncode != 0 or not path.exists() or path.stat().st_size == 0:
                err = stderr.decode(errors="replace").strip() if stderr else ""
                print(f"Camera capture failed (isp rc={proc.returncode}): {err[-200:]}")
                return None
            jpeg_bytes = await asyncio.to_thread(
                self._i420_to_jpeg, path.read_bytes(), int(width), int(height), self.quality)
            if jpeg_bytes:
                print(f"Camera captured {len(jpeg_bytes)} bytes (sunxi_isp {width}x{height})")
            return jpeg_bytes
        except asyncio.TimeoutError:
            print("Camera capture timed out (20s, isp)")
            return None
        except Exception as e:  # noqa: BLE001
            print(f"Camera capture error (isp): {e}")
            return None

    @staticmethod
    def _i420_to_jpeg(buf: bytes, w: int, h: int, quality: int) -> Optional[bytes]:
        """Raw I420 (Y, then U, then V at w/2×h/2) → JPEG bytes (BT.601)."""
        import io
        import numpy as np
        from PIL import Image
        ysz, csz = w * h, (w // 2) * (h // 2)
        if len(buf) < ysz + 2 * csz:
            return None
        a = np.frombuffer(buf, dtype=np.uint8)
        Y = a[:ysz].reshape(h, w).astype(np.float32)
        U = a[ysz:ysz + csz].reshape(h // 2, w // 2).astype(np.float32) - 128.0
        V = a[ysz + csz:ysz + 2 * csz].reshape(h // 2, w // 2).astype(np.float32) - 128.0
        U = np.repeat(np.repeat(U, 2, 0), 2, 1)
        V = np.repeat(np.repeat(V, 2, 0), 2, 1)
        R = (Y + 1.402 * V).clip(0, 255)
        G = (Y - 0.344 * U - 0.714 * V).clip(0, 255)
        B = (Y + 1.772 * U).clip(0, 255)
        rgb = np.stack([R, G, B], axis=2).astype(np.uint8)
        out = io.BytesIO()
        Image.fromarray(rgb, "RGB").save(out, format="JPEG", quality=quality)
        return out.getvalue()
