"""PR-A — durable federation identity (key path resolver + persistence boot guard).

Covers:
- _resolve_key_path precedence: explicit init() > setting > default
- load-or-generate: 32-byte round-trip (script format → loader → same pubkey),
  the loaded-vs-generated signal, and the 33-byte (trailing-newline) rejection
- enforce_persistent_identity boot guard: hard-fail on ephemeral key when required,
  pass when persisted, no-op when the setting is off
"""
from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

import services.federation_identity as fed
from services.federation_identity import (
    _resolve_key_path,
    enforce_persistent_identity,
    get_federation_identity,
    init_federation_identity,
    reset_federation_identity_for_tests,
)
from utils.config import settings


@pytest.fixture(autouse=True)
def _reset():
    reset_federation_identity_for_tests()
    yield
    reset_federation_identity_for_tests()


def _raw32() -> bytes:
    return ed25519.Ed25519PrivateKey.generate().private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )


def _pub(raw: bytes) -> str:
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw().hex()


class TestResolveKeyPath:
    @pytest.mark.unit
    def test_explicit_init_wins(self, tmp_path):
        p = tmp_path / "explicit_key"
        init_federation_identity(p)
        assert _resolve_key_path() == p

    @pytest.mark.unit
    def test_setting_used_when_no_explicit_init(self, tmp_path, monkeypatch):
        reset_federation_identity_for_tests()  # clear any explicit pin
        init_federation_identity(None)          # None → fall through to settings
        p = str(tmp_path / "from_setting")
        monkeypatch.setattr(settings, "federation_identity_key_path", p)
        assert _resolve_key_path() == Path(p)

    @pytest.mark.unit
    def test_default_when_setting_blank(self, monkeypatch):
        init_federation_identity(None)
        monkeypatch.setattr(settings, "federation_identity_key_path", "")
        monkeypatch.setattr(settings, "federation_identity_persisted_key_path", "")
        assert _resolve_key_path() == fed._DEFAULT_KEY_PATH

    @pytest.mark.unit
    def test_persisted_path_preferred_when_file_exists(self, tmp_path, monkeypatch):
        # The RO mounted key (persisted) is preferred over the writable generate path.
        init_federation_identity(None)
        persisted = tmp_path / "mounted" / "federation_identity_key"
        persisted.parent.mkdir()
        persisted.write_bytes(_raw32())
        monkeypatch.setattr(settings, "federation_identity_persisted_key_path", str(persisted))
        monkeypatch.setattr(settings, "federation_identity_key_path", str(tmp_path / "writable_key"))
        assert _resolve_key_path() == persisted

    @pytest.mark.unit
    def test_persisted_path_absent_falls_back_to_writable(self, tmp_path, monkeypatch):
        # Secret not provisioned yet: persisted path set but the file is absent →
        # fall through to the writable generate path (the pre-provision behavior).
        init_federation_identity(None)
        writable = tmp_path / "writable_key"
        monkeypatch.setattr(
            settings, "federation_identity_persisted_key_path", str(tmp_path / "mount" / "absent")
        )
        monkeypatch.setattr(settings, "federation_identity_key_path", str(writable))
        assert _resolve_key_path() == writable


class TestLoadOrGenerate:
    @pytest.mark.unit
    def test_round_trip_loads_persisted_key(self, tmp_path):
        # A pre-existing 32-byte key (the provisioning-script format) loads and
        # yields the SAME pubkey — and is flagged as loaded-from-disk (persisted).
        raw = _raw32()
        keyfile = tmp_path / "federation_identity_key"
        keyfile.write_bytes(raw)  # exactly 32 bytes, no newline
        init_federation_identity(keyfile)
        ident = get_federation_identity()
        assert ident.public_key_hex() == _pub(raw)
        assert fed._loaded_from_disk is True

    @pytest.mark.unit
    def test_generate_marks_ephemeral(self, tmp_path):
        # No key at the path → generate → flagged NOT loaded-from-disk (ephemeral).
        init_federation_identity(tmp_path / "does_not_exist_yet")
        get_federation_identity()
        assert fed._loaded_from_disk is False

    @pytest.mark.unit
    def test_generated_key_persists_within_process(self, tmp_path):
        # First call generates + writes; the file is exactly 32 bytes (so a later
        # boot loads it cleanly — the anti-footgun invariant).
        keyfile = tmp_path / "federation_identity_key"
        init_federation_identity(keyfile)
        get_federation_identity()
        assert keyfile.exists() and len(keyfile.read_bytes()) == 32

    @pytest.mark.unit
    def test_33_byte_key_rejected(self, tmp_path):
        # The trailing-newline footgun: 33 bytes must be rejected, not silently used.
        keyfile = tmp_path / "federation_identity_key"
        keyfile.write_bytes(_raw32() + b"\n")  # 33 bytes
        init_federation_identity(keyfile)
        with pytest.raises(ValueError, match="32"):
            get_federation_identity()

    @pytest.mark.unit
    def test_corrupt_persisted_key_rejected_not_silently_ephemeral(self, tmp_path, monkeypatch):
        # A malformed PROVISIONED key (resolver picks the persisted mount because
        # it exists) must FAIL — never silently fall back to an ephemeral key that
        # would break pairings.
        init_federation_identity(None)
        persisted = tmp_path / "mount" / "federation_identity_key"
        persisted.parent.mkdir()
        persisted.write_bytes(_raw32() + b"\n")  # 33-byte corrupt provisioned key
        monkeypatch.setattr(settings, "federation_identity_persisted_key_path", str(persisted))
        monkeypatch.setattr(settings, "federation_identity_key_path", str(tmp_path / "writable"))
        with pytest.raises(ValueError, match="32"):
            get_federation_identity()


class TestPersistenceBootGuard:
    @pytest.mark.unit
    def test_noop_when_requirement_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "federation_require_persistent_identity", False)
        init_federation_identity(tmp_path / "ephemeral")  # would generate
        enforce_persistent_identity()  # must NOT raise (requirement off)

    @pytest.mark.unit
    def test_hard_fail_on_ephemeral_when_required(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "federation_require_persistent_identity", True)
        init_federation_identity(tmp_path / "ephemeral")  # generates → not persisted
        with pytest.raises(RuntimeError, match="GENERATED at boot"):
            enforce_persistent_identity()

    @pytest.mark.unit
    def test_passes_on_persisted_when_required(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "federation_require_persistent_identity", True)
        keyfile = tmp_path / "federation_identity_key"
        keyfile.write_bytes(_raw32())  # persisted 32-byte key present at boot
        init_federation_identity(keyfile)
        enforce_persistent_identity()  # loaded-from-disk → must NOT raise

    @pytest.mark.unit
    def test_corrupt_persisted_key_surfaces_as_valueerror(self, tmp_path, monkeypatch):
        # The boot guard (require=true) hits a corrupt persisted key → ValueError.
        # lifecycle.py catches (RuntimeError, ValueError) → clean SystemExit(1),
        # never a silent ephemeral fallback. Here we assert the ValueError escapes
        # so the lifecycle handler has something to catch.
        monkeypatch.setattr(settings, "federation_require_persistent_identity", True)
        keyfile = tmp_path / "federation_identity_key"
        keyfile.write_bytes(_raw32() + b"\n")  # corrupt (33 bytes)
        init_federation_identity(keyfile)
        with pytest.raises(ValueError, match="32"):
            enforce_persistent_identity()
