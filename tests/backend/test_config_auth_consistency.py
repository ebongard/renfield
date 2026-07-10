"""Tests for the auth-config consistency validator + RENFIELD_ENV field (#697).

assert_auth_config_consistency hard-fails on AUTH_ENABLED without
WS_AUTH_ENABLED (a phantom security control), warns on soft misconfigs, and is a
no-op for the current auth-off posture. RENFIELD_ENV is now a tracked field.
"""
from pydantic import SecretStr

import pytest

from utils.config import Settings

# A strong key so fail_closed_on_insecure_jwt_key (which runs first) passes and
# the consistency validator is what we actually exercise.
_STRONG = "x" * 48


class TestAuthConfigConsistency:
    @pytest.mark.unit
    def test_auth_on_without_ws_auth_raises(self):
        with pytest.raises(ValueError, match="WS_AUTH_ENABLED"):
            Settings(auth_enabled=True, ws_auth_enabled=False, secret_key=SecretStr(_STRONG))

    @pytest.mark.unit
    def test_auth_on_with_ws_auth_ok(self):
        s = Settings(auth_enabled=True, ws_auth_enabled=True, secret_key=SecretStr(_STRONG))
        assert s.auth_enabled is True and s.ws_auth_enabled is True

    @pytest.mark.unit
    def test_auth_off_is_noop_regardless_of_ws_auth(self):
        # The current single-user posture: everything off → never raises.
        s = Settings(auth_enabled=False, ws_auth_enabled=False)
        assert s.auth_enabled is False

    @pytest.mark.unit
    def test_wildcard_cors_with_auth_warns_not_fatal(self, caplog):
        # WARN, not raise — construction still succeeds.
        s = Settings(
            auth_enabled=True, ws_auth_enabled=True,
            cors_origins="*", secret_key=SecretStr(_STRONG),
        )
        assert s.cors_origins == "*"

    @pytest.mark.unit
    def test_production_with_registration_warns_not_fatal(self, monkeypatch):
        monkeypatch.setenv("RENFIELD_ENV", "production")
        # auth off + strong key so the only signal is the registration WARN.
        s = Settings(allow_registration=True, secret_key=SecretStr(_STRONG))
        assert s.allow_registration is True


class TestRenfieldEnvField:
    @pytest.mark.unit
    def test_default_is_development(self, monkeypatch):
        monkeypatch.delenv("RENFIELD_ENV", raising=False)
        assert Settings().renfield_env == "development"

    @pytest.mark.unit
    def test_reads_from_env(self, monkeypatch):
        monkeypatch.setenv("RENFIELD_ENV", "production")
        # arms the JWT guard → needs a strong key to construct
        s = Settings(secret_key=SecretStr(_STRONG))
        assert s.renfield_env == "production"

    @pytest.mark.unit
    def test_field_drives_jwt_guard(self, monkeypatch):
        """The insecure-key guard now reads the field: production + weak key raises."""
        monkeypatch.setenv("RENFIELD_ENV", "production")
        with pytest.raises(ValueError):
            Settings(auth_enabled=False, secret_key=SecretStr("short"))
