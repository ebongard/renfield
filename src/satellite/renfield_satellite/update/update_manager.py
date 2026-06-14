"""
OTA Update Manager for Renfield Satellite

Handles downloading, verifying, and installing satellite updates with
automatic rollback capability on failure.

Update Flow:
1. Receive update_request from server
2. Download package (0-40%)
3. Verify checksum (40-45%)
4. Create backup (45-55%)
5. Extract package (55-70%)
6. Install (70-90%)
7. Restart service (90-100%)

On any failure after backup creation, the backup is restored automatically.
"""

import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional
import urllib.request


class UpdateStage(str, Enum):
    """Update process stages"""
    IDLE = "idle"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    BACKING_UP = "backing_up"
    EXTRACTING = "extracting"
    INSTALLING = "installing"
    RESTARTING = "restarting"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"


class UpdateError(Exception):
    """Exception raised during update process"""
    def __init__(self, stage: UpdateStage, message: str):
        self.stage = stage
        self.message = message
        super().__init__(f"Update failed at {stage.value}: {message}")


@dataclass
class UpdateRequest:
    """Update request from server"""
    target_version: str
    package_url: str
    checksum: str  # Format: "sha256:hexdigest"
    size_bytes: int


class UpdateManager:
    """
    Manages OTA updates for the satellite.

    Handles the full update lifecycle including download, verification,
    backup, installation, and rollback on failure.
    """

    def __init__(
        self,
        install_path: Optional[str] = None,
        backup_path: Optional[str] = None,
        service_name: str = "renfield-satellite"
    ):
        """
        Initialize UpdateManager.

        Args:
            install_path: Path where satellite is installed
            backup_path: Path for backup during update
            service_name: Name of systemd service to restart
        """
        # Default paths
        if install_path:
            self.install_path = Path(install_path)
        else:
            # Try to detect install path
            self.install_path = self._detect_install_path()

        # Backup path — a SIBLING of the install dir, never inside it. A backup
        # under install_path/.backup gets deleted by the rollback's own
        # rmtree(install_path), so the restore then finds nothing and the whole
        # install (venv included) is lost — this bricked a satellite in prod.
        if backup_path:
            self.backup_path = Path(backup_path)
        else:
            self.backup_path = self.install_path.parent / (
                self.install_path.name + ".update-backup"
            )
        self.service_name = service_name

        # State
        self._is_updating = False
        self._current_stage = UpdateStage.IDLE
        self._progress = 0
        self._on_progress: Optional[Callable[[UpdateStage, int, str], None]] = None
        self._backup_created = False

    def _detect_install_path(self) -> Path:
        """Detect the satellite installation path"""
        # Check common locations
        candidates = [
            Path("/opt/renfield-satellite"),
            Path.home() / "renfield-satellite",
            Path(__file__).parent.parent.parent,  # Development path
        ]

        for path in candidates:
            if path.exists() and (path / "renfield_satellite").exists():
                return path

        # Default to /opt
        return Path("/opt/renfield-satellite")

    @property
    def is_updating(self) -> bool:
        """Check if an update is in progress"""
        return self._is_updating

    @property
    def current_stage(self) -> UpdateStage:
        """Get current update stage"""
        return self._current_stage

    @property
    def progress(self) -> int:
        """Get current progress (0-100)"""
        return self._progress

    def on_progress(self, callback: Callable[[UpdateStage, int, str], None]):
        """
        Register callback for progress updates.

        Args:
            callback: Function(stage, progress, message) to call on updates
        """
        self._on_progress = callback

    def _report_progress(self, stage: UpdateStage, progress: int, message: str = ""):
        """Report progress to callback"""
        self._current_stage = stage
        self._progress = progress
        print(f"[Update] {stage.value}: {progress}% - {message}")
        if self._on_progress:
            self._on_progress(stage, progress, message)

    async def start_update(
        self,
        target_version: str,
        package_url: str,
        checksum: str,
        size_bytes: int,
        base_url: str = ""
    ) -> bool:
        """
        Start the update process.

        Args:
            target_version: Version to update to
            package_url: URL path to download package
            checksum: Expected checksum (sha256:hexdigest)
            size_bytes: Expected package size
            base_url: Base URL of the server (e.g., http://192.168.1.10:8000)

        Returns:
            True if update succeeded, False if failed (rollback will be attempted)
        """
        if self._is_updating:
            print("[Update] Update already in progress")
            return False

        self._is_updating = True
        self._backup_created = False

        # Build full URL
        if package_url.startswith("/"):
            full_url = f"{base_url}{package_url}"
        else:
            full_url = package_url

        request = UpdateRequest(
            target_version=target_version,
            package_url=full_url,
            checksum=checksum,
            size_bytes=size_bytes
        )

        try:
            # Store current version for rollback message
            from renfield_satellite import __version__
            old_version = __version__

            # 1. Download package (0-40%)
            package_path = await self._download_package(request)

            # 2. Verify checksum (40-45%)
            self._verify_checksum(package_path, request.checksum)

            # 3. Create backup (45-55%)
            self._create_backup()

            # 4. Extract package (55-70%)
            extract_path = self._extract_package(package_path)

            # 5. Install (70-90%)
            self._install_package(extract_path)

            # 6. Restart service (90-100%)
            self._report_progress(UpdateStage.RESTARTING, 90, "Restarting service...")

            # Report success before restart (we won't be able to after)
            self._report_progress(UpdateStage.COMPLETED, 100, f"Updated to {target_version}")

            # Trigger restart (this will kill this process)
            await self._restart_service()

            return True

        except UpdateError as e:
            print(f"[Update] Error: {e}")
            self._report_progress(UpdateStage.FAILED, 0, str(e.message))

            # Attempt rollback if backup was created
            if self._backup_created:
                try:
                    self._rollback()
                except Exception as rollback_error:
                    print(f"[Update] Rollback failed: {rollback_error}")

            return False

        except Exception as e:
            print(f"[Update] Unexpected error: {e}")
            self._report_progress(UpdateStage.FAILED, 0, str(e))

            if self._backup_created:
                try:
                    self._rollback()
                except Exception as rollback_error:
                    print(f"[Update] Rollback failed: {rollback_error}")

            return False

        finally:
            self._is_updating = False

    async def _download_package(self, request: UpdateRequest) -> Path:
        """Download the update package"""
        self._report_progress(UpdateStage.DOWNLOADING, 0, "Starting download...")

        temp_dir = Path(tempfile.gettempdir())
        package_path = temp_dir / f"renfield-satellite-{request.target_version}.tar.gz"

        try:
            # Download with progress tracking
            def report_hook(count, block_size, total_size):
                if total_size > 0:
                    progress = int(count * block_size * 40 / total_size)  # 0-40%
                    progress = min(progress, 40)
                    self._report_progress(
                        UpdateStage.DOWNLOADING,
                        progress,
                        f"Downloading... ({count * block_size // 1024}KB / {total_size // 1024}KB)"
                    )

            # Run download in thread pool to avoid blocking
            # Use unverified SSL context for self-signed certs on local network
            import ssl
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            def _download():
                req = urllib.request.Request(request.package_url)
                with urllib.request.urlopen(req, context=ssl_ctx) as response:
                    total_size = int(response.headers.get("Content-Length", 0))
                    with open(package_path, "wb") as f:
                        block_size = 8192
                        count = 0
                        while True:
                            chunk = response.read(block_size)
                            if not chunk:
                                break
                            f.write(chunk)
                            count += 1
                            report_hook(count, block_size, total_size)

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _download)

            self._report_progress(UpdateStage.DOWNLOADING, 40, "Download complete")

            # Verify size
            actual_size = package_path.stat().st_size
            if actual_size != request.size_bytes:
                raise UpdateError(
                    UpdateStage.DOWNLOADING,
                    f"Size mismatch: expected {request.size_bytes}, got {actual_size}"
                )

            return package_path

        except UpdateError:
            raise
        except Exception as e:
            raise UpdateError(UpdateStage.DOWNLOADING, str(e))

    def _verify_checksum(self, package_path: Path, expected_checksum: str):
        """Verify package checksum"""
        self._report_progress(UpdateStage.VERIFYING, 40, "Verifying integrity...")

        try:
            # Parse expected checksum
            if ":" in expected_checksum:
                algo, expected_hash = expected_checksum.split(":", 1)
            else:
                algo = "sha256"
                expected_hash = expected_checksum

            if algo != "sha256":
                raise UpdateError(UpdateStage.VERIFYING, f"Unsupported hash algorithm: {algo}")

            # Calculate actual checksum
            sha256 = hashlib.sha256()
            with open(package_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha256.update(chunk)

            actual_hash = sha256.hexdigest()

            if actual_hash != expected_hash:
                raise UpdateError(
                    UpdateStage.VERIFYING,
                    f"Checksum mismatch: expected {expected_hash[:16]}..., got {actual_hash[:16]}..."
                )

            self._report_progress(UpdateStage.VERIFYING, 45, "Integrity verified")

        except UpdateError:
            raise
        except Exception as e:
            raise UpdateError(UpdateStage.VERIFYING, str(e))

    def _create_backup(self):
        """Back up only the parts of the install the update will replace.

        _install_package swaps out exactly `renfield_satellite/` and
        `requirements.txt` — nothing else. So the backup covers only those, NOT
        the venv / config / models, which the update never touches and which MUST
        survive a rollback. (The old code backed up the whole install_path but
        excluded the venv, then the rollback rmtree'd the whole install_path —
        deleting the venv it had no backup of. Net: a failed update bricked the
        device.) The backup lives outside install_path (see __init__).
        """
        self._report_progress(UpdateStage.BACKING_UP, 45, "Creating backup...")

        try:
            # Remove old backup if exists
            if self.backup_path.exists():
                shutil.rmtree(self.backup_path)

            code_dir = self.install_path / "renfield_satellite"
            if not code_dir.exists():
                # No existing installation to back up
                self._report_progress(UpdateStage.BACKING_UP, 55, "No existing installation, skipping backup")
                return

            self.backup_path.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                code_dir,
                self.backup_path / "renfield_satellite",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
            )
            req = self.install_path / "requirements.txt"
            if req.exists():
                shutil.copy(req, self.backup_path / "requirements.txt")

            self._backup_created = True
            self._report_progress(UpdateStage.BACKING_UP, 55, "Backup created")

        except Exception as e:
            raise UpdateError(UpdateStage.BACKING_UP, str(e))

    def _extract_package(self, package_path: Path) -> Path:
        """Extract the update package with path traversal protection"""
        self._report_progress(UpdateStage.EXTRACTING, 55, "Extracting package...")

        try:
            extract_path = Path(tempfile.mkdtemp())

            with tarfile.open(package_path, "r:gz") as tar:
                self._safe_extract(tar, extract_path)

            self._report_progress(UpdateStage.EXTRACTING, 70, "Package extracted")
            return extract_path

        except UpdateError:
            raise
        except Exception as e:
            raise UpdateError(UpdateStage.EXTRACTING, str(e))

    def _safe_extract(self, tar: tarfile.TarFile, extract_path: Path):
        """Extract tar archive with path traversal protection (Zip Slip prevention)"""
        resolved_base = extract_path.resolve()
        for member in tar.getmembers():
            member_path = (extract_path / member.name).resolve()
            if not str(member_path).startswith(str(resolved_base) + os.sep) and member_path != resolved_base:
                raise UpdateError(
                    UpdateStage.EXTRACTING,
                    f"Path traversal detected in archive: {member.name}"
                )
        # Use data filter on Python 3.12+ for additional safety
        try:
            tar.extractall(extract_path, filter='data')
        except TypeError:
            # Python <3.12 doesn't support filter parameter
            tar.extractall(extract_path)

    def _install_package(self, extract_path: Path):
        """Install the extracted package"""
        self._report_progress(UpdateStage.INSTALLING, 70, "Installing update...")

        try:
            # Find the renfield_satellite directory in extracted files
            satellite_dir = extract_path / "renfield_satellite"
            if not satellite_dir.exists():
                raise UpdateError(
                    UpdateStage.INSTALLING,
                    "Invalid package: renfield_satellite directory not found"
                )

            # Remove old files (keep config)
            target_satellite_dir = self.install_path / "renfield_satellite"
            if target_satellite_dir.exists():
                shutil.rmtree(target_satellite_dir)

            # Copy new files
            shutil.copytree(satellite_dir, target_satellite_dir)
            self._report_progress(UpdateStage.INSTALLING, 80, "Files copied")

            # Copy requirements.txt if present
            requirements = extract_path / "requirements.txt"
            if requirements.exists():
                shutil.copy(requirements, self.install_path / "requirements.txt")

            # Install dependencies if pip is available (with package whitelist)
            self._report_progress(UpdateStage.INSTALLING, 85, "Installing dependencies...")
            req_file = self.install_path / "requirements.txt"
            if req_file.exists():
                try:
                    self._install_requirements(req_file)
                except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
                    # Continue even if pip fails - dependencies might already be installed
                    print("[Update] Warning: pip install failed, continuing anyway")

            self._report_progress(UpdateStage.INSTALLING, 90, "Installation complete")

        except UpdateError:
            raise
        except Exception as e:
            raise UpdateError(UpdateStage.INSTALLING, str(e))

    # Known-safe packages that may appear in satellite requirements.
    # MUST stay in sync with src/satellite/requirements.txt — a missing entry
    # makes the OTA installer reject the satellite's OWN dependency and roll the
    # whole update back (the bug that blocked every OTA: soundcard / pymicro- /
    # pyopen-wakeword / bleak were missing and RPi.GPIO failed name matching).
    # Stored as written; compared after PEP 503 canonicalization, so RPi.GPIO,
    # rpi-gpio and rpi_gpio all match. The regression test parses the real
    # requirements.txt against this set.
    SAFE_PACKAGES = frozenset({
        "websockets", "aiohttp", "numpy", "onnxruntime",
        "openwakeword", "pymicro-wakeword", "pyopen-wakeword",
        "webrtcvad", "noisereduce", "spidev", "soundcard", "bleak",
        "lgpio", "python-mpv", "psutil", "pyyaml", "zeroconf",
        "sounddevice", "pyaudio", "scipy", "librosa", "RPi.GPIO",
    })

    @staticmethod
    def _canonical_pkg(name: str) -> str:
        """PEP 503 canonical project name: lowercase, collapse runs of -_. to -.

        This is the rule pip/PyPI use to resolve a project, so the validator's
        equivalence classes line up with what `pip install` actually fetches.
        Deleting the separators instead (the earlier approach) would let
        `s.o.u.n.d.c.a.r.d` collapse onto `soundcard` while pip resolves it to a
        DISTINCT PyPI project an attacker could register — an OTA RCE bypass.
        """
        return re.sub(r"[-_.]+", "-", name.split("[")[0].strip().lower())

    def _install_requirements(self, req_file: Path):
        """Install requirements with package whitelist validation"""
        safe = {self._canonical_pkg(p) for p in self.SAFE_PACKAGES}
        # Parse and validate packages
        packages = []
        with open(req_file, "r") as f:
            for raw in f:
                # Strip inline comments (PEP 508 `pkg>=1  # note`) and trim.
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                # Reject pip option / flag / file-redirection lines OUTRIGHT.
                # `pip install -r <file>` honours options inside the file, so a
                # silently-skipped `--index-url https://evil/…` would repoint the
                # whole install at an attacker index — bypassing the name
                # allowlist entirely. Fail closed.
                if line.startswith("-"):
                    raise UpdateError(
                        UpdateStage.INSTALLING,
                        f"Disallowed pip option in requirements: {line!r}",
                    )
                # Extract package name (before any version specifier / marker).
                # `@`/whitespace are intentionally NOT split on, so a direct URL
                # reference (`pkg @ https://evil/x.whl`) keeps the URL in the
                # token and fails the allowlist instead of resolving to `pkg`.
                pkg_name = line
                for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", ";"):
                    pkg_name = pkg_name.split(sep)[0]
                packages.append((self._canonical_pkg(pkg_name), line))

        rejected = [(name, spec) for name, spec in packages if name not in safe]
        if rejected:
            rejected_names = [spec for _, spec in rejected]
            print(f"[Update] Rejected unknown packages: {rejected_names}")
            raise UpdateError(
                UpdateStage.INSTALLING,
                f"Unknown packages in requirements: {rejected_names}"
            )

        subprocess.run(
            ["pip", "install", "-r", str(req_file), "--no-deps", "--quiet"],
            check=True,
            timeout=120
        )

    async def _restart_service(self):
        """Restart the satellite service"""
        try:
            # Use subprocess to restart the service
            # This requires passwordless sudo for the specific command
            result = subprocess.run(
                ["sudo", "systemctl", "restart", self.service_name],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                print(f"[Update] Service restart warning: {result.stderr}")
                # Don't raise - the process might be killed before we can check

        except subprocess.TimeoutExpired:
            print("[Update] Service restart timed out")
        except Exception as e:
            print(f"[Update] Service restart error: {e}")
            # Process might be killed by the restart, so don't raise

    def _rollback(self):
        """Restore the code + requirements from the external backup.

        Only the items the update replaced are restored; the venv, config and
        models are left untouched. NEVER rmtree install_path — doing that (with
        an in-tree backup) is exactly what destroyed the install before.
        """
        self._report_progress(UpdateStage.ROLLING_BACK, 0, "Rolling back...")

        try:
            backup_code = self.backup_path / "renfield_satellite"
            if not backup_code.exists():
                print("[Update] No backup to restore")
                return

            # Swap the failed code dir back for the backed-up one.
            target_code = self.install_path / "renfield_satellite"
            if target_code.exists():
                shutil.rmtree(target_code)
            shutil.copytree(backup_code, target_code)

            backup_req = self.backup_path / "requirements.txt"
            if backup_req.exists():
                shutil.copy(backup_req, self.install_path / "requirements.txt")

            print("[Update] Rollback complete")

        except Exception as e:
            print(f"[Update] Rollback error: {e}")
            raise
