"""#1210 — the OTA dependency allowlist comes from the SIGNED package.

The installer used to check the NEW package's requirements.txt against the
RUNNING code's SAFE_PACKAGES, so a release that adds a dependency could never
be delivered by OTA. Now the allowlist is a data file inside the package, and
the new package's copy is used only when the release signature verified in the
same run AND the file is listed in the signed manifest with a matching hash.
Every other case falls back to the running list (fail closed).

These tests drive the real ``start_update`` flow (extract → verify → select
allowlist → backup → install); only the network download, pip and the service
restart are stubbed.
"""
import ast
import asyncio
import base64
import hashlib
import json
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from renfield_satellite.update import update_manager as um_module
from renfield_satellite.update.release_manifest import build_manifest, canonical_bytes
from renfield_satellite.update.update_manager import (
    SAFE_PACKAGES_RELPATH,
    UpdateError,
    UpdateManager,
    parse_safe_packages,
)

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")

VERSION = "9.9.9"
NEW_DEP = "brandnewdep"  # deliberately NOT in the running allowlist


def _keypair():
    priv = ed25519.Ed25519PrivateKey.generate()
    return priv, priv.public_key().public_bytes_raw().hex()


def _running_list_text() -> str:
    return (Path(um_module.__file__).with_name("safe_packages.txt")).read_text(encoding="utf-8")


def _make_source(root: Path, *, allowlist: str | None, requirements: str) -> Path:
    """A package source tree; ``allowlist=None`` ships no safe_packages.txt."""
    pkg = root / "renfield_satellite"
    (pkg / "update").mkdir(parents=True)
    (pkg / "__init__.py").write_text(f'__version__ = "{VERSION}"\n')
    (pkg / "update" / "__init__.py").write_text("")
    if allowlist is not None:
        (root / SAFE_PACKAGES_RELPATH).write_text(allowlist)
    (root / "requirements.txt").write_text(requirements)
    return root


def _sign(root: Path, priv, *, drop: str | None = None) -> tuple[str, str]:
    manifest = build_manifest(root, VERSION)
    if drop:
        manifest["files"].pop(drop)
    body = canonical_bytes(manifest)
    return body.decode("utf-8"), base64.b64encode(priv.sign(body)).decode()


def _tar(root: Path, out: Path) -> Path:
    with tarfile.open(out, "w:gz") as tar:
        tar.add(root / "renfield_satellite", arcname="renfield_satellite")
        tar.add(root / "requirements.txt", arcname="requirements.txt")
    return out


def _run_update(tmp_path, package: Path, *, pubkeys, require, manifest=None, signature=None):
    """Run start_update against ``package``; returns (ok, manager, pip_mock)."""
    install = tmp_path / "install"
    (install / "renfield_satellite").mkdir(parents=True)
    (install / "renfield_satellite" / "__init__.py").write_text('__version__ = "1.0.0"\n')
    mgr = UpdateManager(
        install_path=str(install),
        backup_path=str(tmp_path / "backup"),
        release_pubkeys=pubkeys,
        require_signature=require,
    )
    checksum = "sha256:" + hashlib.sha256(package.read_bytes()).hexdigest()
    with patch.object(mgr, "_download_package", AsyncMock(return_value=package)), \
         patch.object(mgr, "_restart_service", AsyncMock()), \
         patch("subprocess.run", MagicMock(return_value=MagicMock(returncode=0))) as pip:
        ok = asyncio.run(mgr.start_update(
            VERSION, "/pkg", checksum, package.stat().st_size,
            manifest=manifest, signature=signature,
        ))
    return ok, mgr, pip


def _pip_calls(pip) -> list:
    return [c for c in pip.call_args_list if c.args and c.args[0][:1] == ["pip"]]


SIGNED_LIST = _running_list_text() + f"\n{NEW_DEP}\n"
REQS_WITH_NEW_DEP = f"websockets>=12.0\n{NEW_DEP}>=1.0\n"


@pytest.mark.satellite
class TestSignedAllowlistEndToEnd:
    def test_negative_control_new_dep_not_in_running_list(self):
        """Without this, the 'rejected' tests below would prove nothing."""
        canon = {UpdateManager._canonical_pkg(p) for p in UpdateManager.SAFE_PACKAGES}
        assert NEW_DEP not in canon
        assert "websockets" in canon

    def test_signed_package_may_add_a_dependency(self, tmp_path):
        priv, pub = _keypair()
        src = _make_source(tmp_path / "src", allowlist=SIGNED_LIST, requirements=REQS_WITH_NEW_DEP)
        manifest, sig = _sign(src, priv)
        assert SAFE_PACKAGES_RELPATH in json.loads(manifest)["files"]
        pkg = _tar(src, tmp_path / "p.tar.gz")

        ok, mgr, pip = _run_update(tmp_path, pkg, pubkeys=[pub], require=True,
                                   manifest=manifest, signature=sig)

        assert ok, mgr.last_error
        calls = _pip_calls(pip)
        assert len(calls) == 1 and "--no-deps" in calls[0].args[0]
        # The running list is untouched by an OTA (in-memory, loaded at import).
        assert NEW_DEP not in UpdateManager.SAFE_PACKAGES

    def test_unsigned_package_uses_the_running_list(self, tmp_path):
        src = _make_source(tmp_path / "src", allowlist=SIGNED_LIST, requirements=REQS_WITH_NEW_DEP)
        pkg = _tar(src, tmp_path / "p.tar.gz")

        ok, mgr, pip = _run_update(tmp_path, pkg, pubkeys=[], require=False)

        assert not ok
        assert "Unknown packages" in mgr.last_error and NEW_DEP in mgr.last_error
        assert _pip_calls(pip) == []
        assert mgr.rolled_back  # failed at install → the backup was restored

    def test_unsigned_package_with_running_deps_still_installs(self, tmp_path):
        """Positive control for the unsigned path: it is the list, not the path, that refuses."""
        src = _make_source(tmp_path / "src", allowlist=SIGNED_LIST, requirements="websockets>=12.0\n")
        pkg = _tar(src, tmp_path / "p.tar.gz")

        ok, mgr, pip = _run_update(tmp_path, pkg, pubkeys=[], require=False)

        assert ok, mgr.last_error
        assert len(_pip_calls(pip)) == 1

    def test_signed_manifest_without_the_allowlist_uses_the_running_list(self, tmp_path):
        """A signed release that ships no allowlist (an older release format)."""
        priv, pub = _keypair()
        src = _make_source(tmp_path / "src", allowlist=None, requirements=REQS_WITH_NEW_DEP)
        manifest, sig = _sign(src, priv)
        assert SAFE_PACKAGES_RELPATH not in json.loads(manifest)["files"]
        pkg = _tar(src, tmp_path / "p.tar.gz")

        ok, mgr, pip = _run_update(tmp_path, pkg, pubkeys=[pub], require=True,
                                   manifest=manifest, signature=sig)

        assert not ok
        assert "Unknown packages" in mgr.last_error
        assert _pip_calls(pip) == []

    def test_unlisted_allowlist_file_on_disk_aborts_before_install(self, tmp_path):
        """The file is in the tarball but the signed manifest omits it → never trusted."""
        priv, pub = _keypair()
        src = _make_source(tmp_path / "src", allowlist=SIGNED_LIST, requirements=REQS_WITH_NEW_DEP)
        manifest, sig = _sign(src, priv, drop=SAFE_PACKAGES_RELPATH)
        pkg = _tar(src, tmp_path / "p.tar.gz")

        ok, mgr, pip = _run_update(tmp_path, pkg, pubkeys=[pub], require=True,
                                   manifest=manifest, signature=sig)

        assert not ok
        assert "unexpected file" in mgr.last_error and "safe_packages.txt" in mgr.last_error
        assert _pip_calls(pip) == []
        assert not mgr.rolled_back  # aborted before the backup/install

    def test_allowlist_tampered_after_signing_aborts_before_install(self, tmp_path):
        priv, pub = _keypair()
        src = _make_source(tmp_path / "src", allowlist=_running_list_text(),
                           requirements=REQS_WITH_NEW_DEP)
        manifest, sig = _sign(src, priv)
        # Attacker appends the new dependency AFTER signing.
        (src / SAFE_PACKAGES_RELPATH).write_text(SIGNED_LIST)
        pkg = _tar(src, tmp_path / "p.tar.gz")

        ok, mgr, pip = _run_update(tmp_path, pkg, pubkeys=[pub], require=True,
                                   manifest=manifest, signature=sig)

        assert not ok
        assert "hash mismatch" in mgr.last_error and "safe_packages.txt" in mgr.last_error
        assert _pip_calls(pip) == []
        assert not mgr.rolled_back
        # Install dir untouched: still the old code.
        assert '1.0.0' in (tmp_path / "install" / "renfield_satellite" / "__init__.py").read_text()

    def test_listed_allowlist_removed_from_package_aborts_before_install(self, tmp_path):
        """verify_extracted reports a listed-but-missing file — no silent fallback."""
        priv, pub = _keypair()
        src = _make_source(tmp_path / "src", allowlist=SIGNED_LIST, requirements="websockets\n")
        manifest, sig = _sign(src, priv)
        (src / SAFE_PACKAGES_RELPATH).unlink()
        pkg = _tar(src, tmp_path / "p.tar.gz")

        ok, mgr, pip = _run_update(tmp_path, pkg, pubkeys=[pub], require=True,
                                   manifest=manifest, signature=sig)

        assert not ok
        assert "missing file" in mgr.last_error and "safe_packages.txt" in mgr.last_error
        assert _pip_calls(pip) == []

    def test_signed_allowlist_does_not_relax_option_or_url_lines(self, tmp_path):
        """A signed list that names the package does not let an option/URL line through."""
        priv, pub = _keypair()
        for evil in (f"--index-url https://evil.example/simple\n{NEW_DEP}\n",
                     f"{NEW_DEP} @ https://evil.example/x.whl\n"):
            case = tmp_path / str(abs(hash(evil)))
            case.mkdir()
            src = _make_source(case / "src", allowlist=SIGNED_LIST, requirements=evil)
            manifest, sig = _sign(src, priv)
            pkg = _tar(src, case / "p.tar.gz")

            ok, _mgr, pip = _run_update(case, pkg, pubkeys=[pub], require=True,
                                        manifest=manifest, signature=sig)

            assert not ok, evil
            assert _pip_calls(pip) == [], evil


@pytest.mark.satellite
class TestSelectSafePackages:
    """Unit checks of the selection rule, including paths start_update cannot reach."""

    def _mgr(self):
        m = UpdateManager.__new__(UpdateManager)
        return m

    def _tree(self, tmp_path, text: str | bytes) -> tuple[Path, dict]:
        root = tmp_path / "x"
        (root / "renfield_satellite" / "update").mkdir(parents=True)
        data = text.encode() if isinstance(text, str) else text
        (root / SAFE_PACKAGES_RELPATH).write_bytes(data)
        return root, {"version": VERSION,
                      "files": {SAFE_PACKAGES_RELPATH: hashlib.sha256(data).hexdigest()}}

    def test_no_verified_manifest_means_running_list(self, tmp_path):
        root, _ = self._tree(tmp_path, f"{NEW_DEP}\n")
        assert self._mgr()._select_safe_packages(root, None) is UpdateManager.SAFE_PACKAGES

    def test_listed_and_matching_means_signed_list(self, tmp_path):
        root, manifest = self._tree(tmp_path, f"{NEW_DEP}\nnumpy\n")
        assert self._mgr()._select_safe_packages(root, manifest) == frozenset({NEW_DEP, "numpy"})

    def test_not_listed_means_running_list_even_if_file_present(self, tmp_path):
        root, manifest = self._tree(tmp_path, f"{NEW_DEP}\n")
        manifest["files"] = {}
        assert self._mgr()._select_safe_packages(root, manifest) is UpdateManager.SAFE_PACKAGES

    def test_listed_but_missing_on_disk_means_running_list(self, tmp_path):
        root, manifest = self._tree(tmp_path, f"{NEW_DEP}\n")
        (root / SAFE_PACKAGES_RELPATH).unlink()
        assert self._mgr()._select_safe_packages(root, manifest) is UpdateManager.SAFE_PACKAGES

    def test_swapped_after_verification_aborts(self, tmp_path):
        """TOCTOU: the bytes parsed are re-hashed, not trusted from verify_extracted."""
        root, manifest = self._tree(tmp_path, "numpy\n")
        (root / SAFE_PACKAGES_RELPATH).write_text(f"numpy\n{NEW_DEP}\n")
        with pytest.raises(UpdateError, match="does not match the signed manifest"):
            self._mgr()._select_safe_packages(root, manifest)

    @pytest.mark.parametrize("bad", [
        "numpy>=1.0\n",                      # specifier, not a bare name
        "pkg @ https://evil/x.whl\n",        # URL reference
        "--index-url https://evil/simple\n",  # pip option
        "# only a comment\n\n",              # no entries
        b"\xff\xfe numpy\n",                 # not UTF-8
    ])
    def test_invalid_signed_list_means_running_list(self, tmp_path, bad):
        root, manifest = self._tree(tmp_path, bad)
        assert self._mgr()._select_safe_packages(root, manifest) is UpdateManager.SAFE_PACKAGES


@pytest.mark.satellite
class TestAllowlistDataFile:
    def test_running_list_is_loaded_from_the_data_file(self):
        assert UpdateManager.SAFE_PACKAGES == parse_safe_packages(_running_list_text())
        assert "opuslib" in UpdateManager.SAFE_PACKAGES

    def test_no_package_name_literal_left_in_python(self):
        """Single source: the names live in the data file, not in update_manager.py."""
        tree = ast.parse(Path(um_module.__file__).read_text(encoding="utf-8"))
        strings = {n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        for name in ("opuslib", "soundcard", "pyopen-wakeword", "RPi.GPIO"):
            assert name not in strings, name
        # Negative control: the walk does see plain string constants.
        assert "safe_packages.txt" in strings

    def test_parse_rejects_non_names_and_accepts_comments(self):
        assert parse_safe_packages("# c\nnumpy  # inline\n\nRPi.GPIO\n") == frozenset({"numpy", "RPi.GPIO"})
        for bad in ("numpy==1\n", "a b\n", "-r x\n", "pkg[extra]\n", "\n# nothing\n"):
            with pytest.raises(ValueError):
                parse_safe_packages(bad)

    def test_missing_running_file_fails_closed(self, tmp_path, monkeypatch):
        """No allowlist on disk → an empty list (rejects everything), never a crash or allow-all."""
        fake = tmp_path / "update_manager.py"
        monkeypatch.setattr(um_module, "__file__", str(fake))
        assert um_module._load_running_safe_packages() == frozenset()
        (tmp_path / "safe_packages.txt").write_text("numpy\n")
        assert um_module._load_running_safe_packages() == frozenset({"numpy"})

    def test_signer_manifest_lists_the_shipped_allowlist(self):
        """bin/sign_satellite_release.py signs build_manifest(src/satellite) — it must cover the file."""
        src = Path(__file__).resolve().parents[2] / "src" / "satellite"
        if not (src / SAFE_PACKAGES_RELPATH).exists():
            pytest.skip("satellite source tree not present")
        assert SAFE_PACKAGES_RELPATH in build_manifest(src, "0")["files"]
