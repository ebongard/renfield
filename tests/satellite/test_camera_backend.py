"""Tests for the satellite CameraController capture backends (rpicam vs sunxi_isp).

The A733 (Esszimmer) can't capture via rpicam — it uses the sunxi_isp backend
(/opt/awisp/renfield_isp_capture → I420 → JPEG). These pin the config-driven backend
selection + the I420→JPEG conversion so a regression is caught here, not by a dark camera.
No hardware: open() only checks tool availability; capture() is not exercised.
"""

import io
from unittest.mock import patch

import numpy as np
import pytest

from renfield_satellite.hardware.camera import CameraController


class TestBackendSelection:
    @pytest.mark.satellite
    def test_rpicam_unavailable_when_binary_missing(self):
        with patch("shutil.which", return_value=None):
            assert CameraController(backend="rpicam").open() is False

    @pytest.mark.satellite
    def test_rpicam_available_when_binary_present(self):
        with patch("shutil.which", return_value="/usr/bin/rpicam-still"):
            cam = CameraController(backend="rpicam")
            assert cam.open() is True
            assert cam.available is True
            cam.close()

    @pytest.mark.satellite
    def test_isp_unavailable_when_tool_missing(self):
        # sunxi_isp must NOT fall back to rpicam — it checks the ISP tool path.
        with patch("os.path.exists", return_value=False):
            assert CameraController(backend="sunxi_isp").open() is False

    @pytest.mark.satellite
    def test_isp_available_when_tool_present(self):
        with patch("os.path.exists", return_value=True):
            cam = CameraController(backend="sunxi_isp")
            assert cam.open() is True
            cam.close()


class TestI420ToJpeg:
    @staticmethod
    def _synthetic_i420(w: int, h: int) -> bytes:
        y = np.tile(np.linspace(0, 255, w, dtype=np.uint8), (h, 1)).tobytes()
        u = np.full((h // 2, w // 2), 128, dtype=np.uint8).tobytes()
        v = np.full((h // 2, w // 2), 96, dtype=np.uint8).tobytes()
        return y + u + v

    @pytest.mark.satellite
    def test_produces_valid_jpeg_of_right_size(self):
        pytest.importorskip("PIL")
        from PIL import Image
        w, h = 64, 48
        jpeg = CameraController._i420_to_jpeg(self._synthetic_i420(w, h), w, h, 85)
        assert jpeg is not None
        assert jpeg[:2] == b"\xff\xd8"          # JPEG SOI marker
        assert Image.open(io.BytesIO(jpeg)).size == (w, h)

    @pytest.mark.satellite
    def test_short_buffer_returns_none(self):
        pytest.importorskip("PIL")
        # a truncated frame must fail safe (None), never raise into the capture path
        assert CameraController._i420_to_jpeg(b"\x00" * 10, 64, 48, 85) is None
