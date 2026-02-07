"""
OTA Update Security Tests

Tests for path traversal protection and package whitelist validation
in the satellite OTA update manager.
"""

import io
import shutil
import tarfile
import tempfile

import pytest
from unittest.mock import patch, MagicMock

from renfield_satellite.update.update_manager import (
    UpdateError,
    UpdateManager,
)


# ============================================================================
# Path Traversal Protection Tests (_safe_extract)
# ============================================================================


class TestSafeExtract:
    """Tests for _safe_extract() path traversal prevention"""

    @pytest.fixture
    def update_manager(self, tmp_path):
        """Create an UpdateManager with temp paths"""
        install_path = tmp_path / "install"
        install_path.mkdir()
        (install_path / "renfield_satellite").mkdir()
        return UpdateManager(
            install_path=str(install_path),
            backup_path=str(tmp_path / "backup"),
        )

    @pytest.fixture
    def extract_dir(self):
        """Create a temporary extraction directory"""
        path = tempfile.mkdtemp()
        yield path
        shutil.rmtree(path, ignore_errors=True)

    @pytest.mark.satellite
    def test_path_traversal_relative_rejected(self, update_manager, extract_dir):
        """Test: Tarfile member with ../../../etc/passwd raises UpdateError"""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="../../../etc/passwd")
            info.size = 5
            tar.addfile(info, io.BytesIO(b"hello"))
        buf.seek(0)

        from pathlib import Path

        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            with pytest.raises(UpdateError, match="Path traversal"):
                update_manager._safe_extract(tar, Path(extract_dir))

    @pytest.mark.satellite
    def test_path_traversal_absolute_rejected(self, update_manager, extract_dir):
        """Test: Tarfile member with absolute path /etc/shadow raises UpdateError"""
        from pathlib import Path

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="/etc/shadow")
            info.size = 5
            tar.addfile(info, io.BytesIO(b"hello"))
        buf.seek(0)

        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            with pytest.raises(UpdateError, match="Path traversal"):
                update_manager._safe_extract(tar, Path(extract_dir))

    @pytest.mark.satellite
    def test_normal_file_extracts_successfully(self, update_manager, extract_dir):
        """Test: Normal tarfile with renfield_satellite/main.py extracts OK"""
        from pathlib import Path

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="renfield_satellite/main.py")
            content = b"# main module\n"
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        buf.seek(0)

        extract_path = Path(extract_dir)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            update_manager._safe_extract(tar, extract_path)

        # Verify the file was actually extracted
        extracted_file = extract_path / "renfield_satellite" / "main.py"
        assert extracted_file.exists()
        assert extracted_file.read_text() == "# main module\n"


# ============================================================================
# Package Whitelist Validation Tests (_install_requirements)
# ============================================================================


class TestInstallRequirements:
    """Tests for _install_requirements() package whitelist validation"""

    @pytest.fixture
    def update_manager(self, tmp_path):
        """Create an UpdateManager with temp paths"""
        install_path = tmp_path / "install"
        install_path.mkdir()
        (install_path / "renfield_satellite").mkdir()
        return UpdateManager(
            install_path=str(install_path),
            backup_path=str(tmp_path / "backup"),
        )

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_valid_packages_pass_validation(self, mock_run, update_manager, tmp_path):
        """Test: Requirements with whitelisted packages pass validation"""
        mock_run.return_value = MagicMock(returncode=0)

        req_file = tmp_path / "requirements.txt"
        req_file.write_text("websockets>=10.0\naiohttp\n")

        # Should not raise
        update_manager._install_requirements(req_file)

        # Verify pip was called
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert "pip" in call_args[0][0]
        assert "--no-deps" in call_args[0][0]

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_unknown_packages_rejected(self, mock_run, update_manager, tmp_path):
        """Test: Requirements with unknown packages raise UpdateError"""
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("evil-package==1.0\n")

        with pytest.raises(UpdateError, match="Unknown packages"):
            update_manager._install_requirements(req_file)

        # pip should NOT have been called
        mock_run.assert_not_called()

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_mixed_packages_rejected(self, mock_run, update_manager, tmp_path):
        """Test: Mix of valid and invalid packages still raises UpdateError"""
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("websockets>=10.0\nevil-package==1.0\naiohttp\n")

        with pytest.raises(UpdateError, match="Unknown packages"):
            update_manager._install_requirements(req_file)

        mock_run.assert_not_called()


# ============================================================================
# SAFE_PACKAGES Whitelist Tests
# ============================================================================


class TestSafePackagesWhitelist:
    """Tests for the SAFE_PACKAGES constant"""

    @pytest.mark.satellite
    def test_whitelist_contains_expected_packages(self):
        """Test: SAFE_PACKAGES contains all expected core satellite packages"""
        expected = [
            "websockets",
            "aiohttp",
            "numpy",
            "openwakeword",
            "psutil",
            "pyyaml",
            "zeroconf",
            "pyaudio",
        ]
        for pkg in expected:
            assert pkg in UpdateManager.SAFE_PACKAGES, (
                f"Expected '{pkg}' in SAFE_PACKAGES"
            )

    @pytest.mark.satellite
    def test_whitelist_is_frozenset(self):
        """Test: SAFE_PACKAGES is a frozenset (immutable)"""
        assert isinstance(UpdateManager.SAFE_PACKAGES, frozenset)

    @pytest.mark.satellite
    def test_whitelist_does_not_contain_dangerous_packages(self):
        """Test: SAFE_PACKAGES does not contain obviously dangerous packages"""
        dangerous = ["pip", "setuptools", "requests", "paramiko", "cryptography"]
        for pkg in dangerous:
            assert pkg not in UpdateManager.SAFE_PACKAGES, (
                f"'{pkg}' should not be in SAFE_PACKAGES"
            )
