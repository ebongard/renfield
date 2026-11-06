"""
Model Downloader for Renfield Satellite

Downloads wake word models from the Renfield backend server
when they are not available locally.
"""

import hashlib
import os
import ssl
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urljoin

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None
    AIOHTTP_AVAILABLE = False
    print("Warning: aiohttp not installed. Model download disabled.")


class ModelDownloader:
    """
    Downloads wake word models from the backend server.

    Models are saved to the satellite's models directory and can be
    loaded by the wake word detector.
    """

    def __init__(
        self,
        models_path: str = "/opt/renfield-satellite/models",
        server_base_url: Optional[str] = None,
    ):
        """
        Initialize model downloader.

        Args:
            models_path: Local directory to save models
            server_base_url: Base URL of the Renfield backend (e.g., "http://server:8000")
        """
        self.models_path = Path(models_path)
        self.server_base_url = server_base_url
        self._auth_token: Optional[str] = None
        self._verify_tls: bool = True

        # Ensure models directory exists
        self.models_path.mkdir(parents=True, exist_ok=True)

    def set_server_url(self, url: str):
        """Set the server base URL (e.g., "http://server:8000")"""
        # Convert WebSocket URL to HTTP if needed
        if url.startswith("ws://"):
            url = url.replace("ws://", "http://")
        elif url.startswith("wss://"):
            url = url.replace("wss://", "https://")
        # Remove /ws/satellite path if present
        if "/ws/satellite" in url:
            url = url.replace("/ws/satellite", "")
        self.server_base_url = url

    def set_auth_token(self, token: str):
        """Set authentication token for API requests"""
        self._auth_token = token

    def set_verify_tls(self, verify: bool):
        """Set whether to verify TLS certificates."""
        self._verify_tls = verify

    def is_model_available(self, model_id: str) -> bool:
        """
        Check if a model is available locally.

        Args:
            model_id: The model identifier (e.g., "alexa")

        Returns:
            True if the model file exists locally
        """
        # Check for TFLite model
        tflite_path = self.models_path / f"{model_id}.tflite"
        if tflite_path.exists():
            return True

        # Check for ONNX model (legacy)
        onnx_path = self.models_path / f"{model_id}.onnx"
        if onnx_path.exists():
            return True

        # Check with version suffix
        for ext in [".tflite", ".onnx"]:
            versioned_path = self.models_path / f"{model_id}_v0.1{ext}"
            if versioned_path.exists():
                return True

        return False

    def get_model_path(self, model_id: str) -> Optional[Path]:
        """
        Get the path to a local model file.

        Args:
            model_id: The model identifier

        Returns:
            Path to the model file if it exists, None otherwise
        """
        # Check for TFLite model
        tflite_path = self.models_path / f"{model_id}.tflite"
        if tflite_path.exists():
            return tflite_path

        # Check for ONNX model
        onnx_path = self.models_path / f"{model_id}.onnx"
        if onnx_path.exists():
            return onnx_path

        # Check with version suffix
        for ext in [".tflite", ".onnx"]:
            versioned_path = self.models_path / f"{model_id}_v0.1{ext}"
            if versioned_path.exists():
                return versioned_path

        return None

    def local_sha256(self, model_id: str) -> Optional[str]:
        """SHA-256 (hex) of the local model file, or None if there is none."""
        path = self.get_model_path(model_id)
        if path is None:
            return None
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    def is_model_stale(self, model_id: str, expected_sha256: Optional[str]) -> bool:
        """True when a local file exists but differs from the server's.

        Without an expected checksum (an older server) nothing is stale.
        """
        if not expected_sha256 or not self.is_model_available(model_id):
            return False
        return self.local_sha256(model_id) != expected_sha256

    async def download_model(
        self, model_id: str, expected_sha256: Optional[str] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Download a model from the backend server.

        With ``expected_sha256`` the download must match it, and only then
        replaces the local file (atomically) — a failed or wrong download
        leaves the current model in place.

        Args:
            model_id: The model identifier (e.g., "alexa", "hey_jarvis")
            expected_sha256: SHA-256 the server announced for this model

        Returns:
            Tuple of (success: bool, error_message: Optional[str])
        """
        if not AIOHTTP_AVAILABLE:
            return False, "aiohttp not installed - cannot download models"

        if not self.server_base_url:
            return False, "Server URL not configured"

        download_url = f"{self.server_base_url}/api/settings/wakeword/models/{model_id}"

        print(f"📥 Downloading model: {model_id} from {download_url}")

        try:
            headers = {}
            if self._auth_token:
                headers["Authorization"] = f"Bearer {self._auth_token}"

            # Configure SSL context for https:// URLs
            ssl_ctx = None
            if download_url.startswith("https://"):
                if self._verify_tls:
                    ssl_ctx = ssl.create_default_context()
                else:
                    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    ssl_ctx.check_hostname = False
                    ssl_ctx.verify_mode = ssl.CERT_NONE

            async with aiohttp.ClientSession() as session:
                async with session.get(download_url, headers=headers, ssl=ssl_ctx) as response:
                    if response.status == 200:
                        # Determine file extension from Content-Disposition header
                        cd = response.headers.get("Content-Disposition", "")
                        ext = ".onnx"  # Default to ONNX
                        if "filename=" in cd:
                            fname = cd.split("filename=")[-1].strip('"')
                            ext = Path(fname).suffix or ".onnx"
                        content = await response.read()
                        if expected_sha256:
                            got = hashlib.sha256(content).hexdigest()
                            if got != expected_sha256:
                                return False, (
                                    f"Checksum mismatch for {model_id}: "
                                    f"got {got[:12]}, expected {expected_sha256[:12]}"
                                )
                        # Save model file: write aside, then rename, so the
                        # detector never sees a half-written model.
                        model_path = self.models_path / f"{model_id}{ext}"
                        tmp_path = model_path.with_name(model_path.name + ".part")
                        tmp_path.write_bytes(content)
                        os.replace(tmp_path, model_path)
                        print(f"✅ Model downloaded: {model_id} ({len(content)} bytes) -> {model_path.name}")
                        return True, None

                    elif response.status == 404:
                        return False, f"Model not found on server: {model_id}"

                    elif response.status == 401 or response.status == 403:
                        return False, f"Authentication failed for model download"

                    else:
                        error_text = await response.text()
                        return False, f"Download failed ({response.status}): {error_text[:100]}"

        except aiohttp.ClientError as e:
            return False, f"Network error: {str(e)}"
        except Exception as e:
            return False, f"Download error: {str(e)}"

    async def ensure_models_available(
        self,
        model_ids: list[str]
    ) -> Tuple[list[str], list[str]]:
        """
        Ensure all requested models are available, downloading if needed.

        Args:
            model_ids: List of model IDs to check/download

        Returns:
            Tuple of (available_models, failed_models)
        """
        available = []
        failed = []

        for model_id in model_ids:
            if self.is_model_available(model_id):
                print(f"✓ Model available locally: {model_id}")
                available.append(model_id)
            else:
                # Try to download
                success, error = await self.download_model(model_id)
                if success:
                    available.append(model_id)
                else:
                    print(f"✗ Model unavailable: {model_id} - {error}")
                    failed.append(model_id)

        return available, failed


# Singleton instance
_model_downloader: Optional[ModelDownloader] = None


def get_model_downloader() -> ModelDownloader:
    """Get or create the model downloader singleton"""
    global _model_downloader
    if _model_downloader is None:
        _model_downloader = ModelDownloader()
    return _model_downloader
