"""
Satellite OTA Update Service

Handles building and serving update packages for satellite voice assistants.
Manages update initiation and tracks update progress.
"""

import hashlib
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Optional, Dict, Any
from loguru import logger

from utils.config import settings
from services.satellite_manager import get_satellite_manager, UpdateStatus


class SatelliteUpdateService:
    """
    Service for managing satellite OTA updates.

    Responsibilities:
    - Build update packages from satellite source code
    - Serve update packages to satellites
    - Initiate updates via WebSocket
    - Track update progress
    """

    def __init__(self):
        # Path to satellite source code
        self.satellite_source_path = Path("/app/satellite")
        # Cache for built packages
        self._package_cache: Optional[Dict[str, Any]] = None
        self._package_cache_time: float = 0
        self._package_cache_ttl: float = settings.satellite_package_cache_ttl

        logger.info("📦 SatelliteUpdateService initialized")

    def get_latest_version(self) -> str:
        """Get the latest available satellite version from config"""
        return settings.satellite_latest_version

    def is_update_available(self, current_version: str) -> bool:
        """
        Check if an update is available for the given version.

        Args:
            current_version: Current satellite version (e.g., "1.0.0")

        Returns:
            True if a newer version is available
        """
        if current_version == "unknown":
            return False

        latest = self.get_latest_version()

        try:
            current_parts = [int(x) for x in current_version.split(".")]
            latest_parts = [int(x) for x in latest.split(".")]

            # Pad with zeros
            while len(current_parts) < len(latest_parts):
                current_parts.append(0)
            while len(latest_parts) < len(current_parts):
                latest_parts.append(0)

            return latest_parts > current_parts
        except (ValueError, AttributeError):
            return False

    def _get_satellite_source_files(self) -> list:
        """Get list of files to include in the update package"""
        files = []
        source_path = self.satellite_source_path

        if not source_path.exists():
            logger.warning(f"⚠️ Satellite source path not found: {source_path}")
            return files

        # Include Python files and config
        patterns = [
            "renfield_satellite/**/*.py",
            "renfield_satellite/**/*.yaml",
            "renfield_satellite/**/*.json",
            "requirements.txt",
            "setup.py",
            "pyproject.toml",
        ]

        import glob
        for pattern in patterns:
            full_pattern = str(source_path / pattern)
            matched = glob.glob(full_pattern, recursive=True)
            files.extend(matched)

        return files

    def build_update_package(self) -> Optional[Path]:
        """
        Build an update package (tarball) from the satellite source.

        Returns:
            Path to the tarball, or None if build fails
        """
        # Check cache
        now = time.time()
        if (
            self._package_cache
            and now - self._package_cache_time < self._package_cache_ttl
            and self._package_cache.get("path")
            and Path(self._package_cache["path"]).exists()
        ):
            return Path(self._package_cache["path"])

        source_path = self.satellite_source_path
        if not source_path.exists():
            logger.error(f"❌ Satellite source not found at {source_path}")
            return None

        try:
            # Create temporary tarball
            temp_dir = Path(tempfile.gettempdir())
            version = self.get_latest_version()
            tarball_name = f"renfield-satellite-{version}.tar.gz"
            tarball_path = temp_dir / tarball_name

            logger.info(f"📦 Building update package: {tarball_path}")

            with tarfile.open(tarball_path, "w:gz") as tar:
                # Add the renfield_satellite directory
                satellite_dir = source_path / "renfield_satellite"
                if satellite_dir.exists():
                    tar.add(satellite_dir, arcname="renfield_satellite")

                # Add requirements.txt if it exists
                requirements = source_path / "requirements.txt"
                if requirements.exists():
                    tar.add(requirements, arcname="requirements.txt")

                # Add setup.py if it exists
                setup_py = source_path / "setup.py"
                if setup_py.exists():
                    tar.add(setup_py, arcname="setup.py")

                # Add pyproject.toml if it exists
                pyproject = source_path / "pyproject.toml"
                if pyproject.exists():
                    tar.add(pyproject, arcname="pyproject.toml")

            # Calculate checksum
            checksum = self._calculate_checksum(tarball_path)
            size = tarball_path.stat().st_size

            # Update cache
            self._package_cache = {
                "path": str(tarball_path),
                "checksum": checksum,
                "size": size,
                "version": version
            }
            self._package_cache_time = now

            logger.info(f"✅ Update package built: {tarball_path} ({size} bytes, {checksum[:16]}...)")
            return tarball_path

        except Exception as e:
            logger.error(f"❌ Failed to build update package: {e}")
            return None

    def _calculate_checksum(self, file_path: Path) -> str:
        """Calculate SHA256 checksum of a file"""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return f"sha256:{sha256.hexdigest()}"

    def get_package_info(self) -> Optional[Dict[str, Any]]:
        """
        Get information about the current update package.

        Returns:
            Dict with path, checksum, size, version, or None if not available
        """
        # Build package if not cached
        package_path = self.build_update_package()
        if not package_path:
            return None

        return self._package_cache

    async def initiate_update(self, satellite_id: str) -> Dict[str, Any]:
        """
        Initiate an update for a specific satellite.

        Args:
            satellite_id: ID of the satellite to update

        Returns:
            Dict with success status and message
        """
        manager = get_satellite_manager()
        sat = manager.get_satellite(satellite_id)

        if not sat:
            return {
                "success": False,
                "message": f"Satellite '{satellite_id}' not found or not connected"
            }

        # Check if update is available
        if not self.is_update_available(sat.version):
            return {
                "success": False,
                "message": f"No update available (current: {sat.version}, latest: {self.get_latest_version()})"
            }

        # Check if already updating
        if sat.update_status == UpdateStatus.IN_PROGRESS:
            return {
                "success": False,
                "message": "Update already in progress"
            }

        # Get package info
        package_info = self.get_package_info()
        if not package_info:
            return {
                "success": False,
                "message": "Failed to build update package"
            }

        # Update status
        manager.set_update_status(
            satellite_id,
            UpdateStatus.IN_PROGRESS,
            stage="initiating",
            progress=0
        )

        # Send update request to satellite
        try:
            await sat.websocket.send_json({
                "type": "update_request",
                "target_version": package_info["version"],
                "package_url": "/api/satellites/update-package",
                "checksum": package_info["checksum"],
                "size_bytes": package_info["size"]
            })

            logger.info(f"📤 Update request sent to satellite {satellite_id}")
            return {
                "success": True,
                "message": f"Update to v{package_info['version']} initiated",
                "target_version": package_info["version"]
            }

        except Exception as e:
            manager.set_update_status(
                satellite_id,
                UpdateStatus.FAILED,
                stage="initiating",
                progress=0,
                error=str(e)
            )
            return {
                "success": False,
                "message": f"Failed to send update request: {e}"
            }


# Global singleton instance
_satellite_update_service: Optional[SatelliteUpdateService] = None


def get_satellite_update_service() -> SatelliteUpdateService:
    """Get or create the global SatelliteUpdateService instance"""
    global _satellite_update_service
    if _satellite_update_service is None:
        _satellite_update_service = SatelliteUpdateService()
    return _satellite_update_service
