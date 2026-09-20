"""Tests for the auth-config consistency validator + RENFIELD_ENV field (#697).

assert_auth_config_consistency warns on soft misconfigs and is a no-op for the
current auth-off posture. RENFIELD_ENV is a tracked field.

The former hard fail — AUTH_ENABLED without WS_AUTH_ENABLED, a phantom security
control — is gone with the second flag itself: AUTH_ENABLED is now the single
auth posture. What remains of that check (a leftover, contradicting
WS_AUTH_ENABLED key) lives in test_config_single_auth_flag.py.
"""
from loguru import logger as loguru_logger
from pydantic import SecretStr

import pytest

from utils.config import Settings

# A strong key so fail_closed_on_insecure_jwt_key (which runs first) passes and
# the consistency validator is what we actually exercise.
_STRONG = "x" * 48


class TestAuthConfigConsistency:
    @pytest.mark.unit
    def test_auth_on_constructs(self):
        s = Settings(auth_enabled=True, secret_key=SecretStr(_STRONG))
        assert s.auth_enabled is True

    @pytest.mark.unit
    def test_auth_off_is_noop(self):
        # The current single-user posture: everything off → never raises.
        s = Settings(auth_enabled=False)
        assert s.auth_enabled is False

    @pytest.mark.unit
    def test_wildcard_cors_with_auth_warns_not_fatal(self, caplog):
        # WARN, not raise — construction still succeeds.
        s = Settings(
            auth_enabled=True,
            cors_origins="*", secret_key=SecretStr(_STRONG),
        )
        assert s.cors_origins == "*"

    @pytest.mark.unit
    def test_production_with_registration_warns_not_fatal(self, monkeypatch):
        monkeypatch.setenv("RENFIELD_ENV", "production")
        # auth off + strong key so the only signal is the registration WARN.
        s = Settings(allow_registration=True, secret_key=SecretStr(_STRONG))
        assert s.allow_registration is True

    # --- BL-0124: an authenticated production instance must DECIDE on signup ---
    # The code default is True (dev + the auth-off household). An auth-on
    # production/staging instance that merely forgot the key would open
    # self-registration to the internet — that inherited default is a boot error,
    # an explicit value (either way) is the operator's call.

    @pytest.mark.unit
    def test_production_auth_on_with_inherited_registration_default_refuses_to_boot(self, monkeypatch):
        monkeypatch.setenv("RENFIELD_ENV", "production")
        monkeypatch.delenv("ALLOW_REGISTRATION", raising=False)
        with pytest.raises(ValueError, match="ALLOW_REGISTRATION unset"):
            Settings(auth_enabled=True, secret_key=SecretStr(_STRONG))

    @pytest.mark.unit
    def test_staging_auth_on_with_inherited_registration_default_refuses_to_boot(self, monkeypatch):
        monkeypatch.setenv("RENFIELD_ENV", "staging")
        monkeypatch.delenv("ALLOW_REGISTRATION", raising=False)
        with pytest.raises(ValueError, match="ALLOW_REGISTRATION unset"):
            Settings(auth_enabled=True, secret_key=SecretStr(_STRONG))

    @pytest.mark.unit
    def test_production_auth_on_with_explicit_open_registration_warns_not_fatal(self, monkeypatch):
        monkeypatch.setenv("RENFIELD_ENV", "production")
        # Explicit opt-in (constructor kwarg) — the operator decided; WARN only,
        # and the WARN must actually be emitted (loguru sink, not stdlib caplog).
        warnings: list[str] = []
        sink = loguru_logger.add(lambda m: warnings.append(str(m)), level="WARNING")
        try:
            s = Settings(auth_enabled=True, allow_registration=True, secret_key=SecretStr(_STRONG))
        finally:
            loguru_logger.remove(sink)
        assert s.allow_registration is True
        assert any("ALLOW_REGISTRATION=true" in w for w in warnings), warnings

    @pytest.mark.unit
    def test_production_auth_on_with_registration_set_via_env_counts_as_explicit(self, monkeypatch):
        monkeypatch.setenv("RENFIELD_ENV", "production")
        # A settings source (env / .env) populates model_fields_set — the real
        # deployment path (ConfigMap → env) must count as "decided".
        monkeypatch.setenv("ALLOW_REGISTRATION", "true")
        s = Settings(auth_enabled=True, secret_key=SecretStr(_STRONG))
        assert s.allow_registration is True

    @pytest.mark.unit
    def test_production_auth_on_with_registration_off_is_silent(self, monkeypatch):
        monkeypatch.setenv("RENFIELD_ENV", "production")
        s = Settings(auth_enabled=True, allow_registration=False, secret_key=SecretStr(_STRONG))
        assert s.allow_registration is False

    @pytest.mark.unit
    def test_production_auth_on_with_registration_off_via_env_is_silent(self, monkeypatch):
        # The xidra path exactly: ConfigMap → env → "false".
        monkeypatch.setenv("RENFIELD_ENV", "production")
        monkeypatch.setenv("ALLOW_REGISTRATION", "false")
        s = Settings(auth_enabled=True, secret_key=SecretStr(_STRONG))
        assert s.allow_registration is False

    @pytest.mark.unit
    @pytest.mark.parametrize("env_value", ["prod", "PRODUCTION", " staging "])
    def test_real_env_spellings_arm_the_gate(self, monkeypatch, env_value):
        monkeypatch.setenv("RENFIELD_ENV", env_value)
        monkeypatch.delenv("ALLOW_REGISTRATION", raising=False)
        with pytest.raises(ValueError, match="ALLOW_REGISTRATION unset"):
            Settings(auth_enabled=True, secret_key=SecretStr(_STRONG))

    @pytest.mark.unit
    def test_value_from_dotenv_file_counts_as_explicit(self, monkeypatch, tmp_path):
        # The other real settings source: a .env file (build box / dev).
        monkeypatch.setenv("RENFIELD_ENV", "production")
        monkeypatch.delenv("ALLOW_REGISTRATION", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("ALLOW_REGISTRATION=false\n")
        s = Settings(_env_file=env_file, auth_enabled=True, secret_key=SecretStr(_STRONG))
        assert s.allow_registration is False

    @pytest.mark.unit
    def test_production_auth_off_with_inherited_default_still_boots(self, monkeypatch):
        # Auth off = single trust domain; open registration is meaningless there
        # and must not block a boot (the household posture at a future env flip).
        monkeypatch.setenv("RENFIELD_ENV", "production")
        monkeypatch.delenv("ALLOW_REGISTRATION", raising=False)
        s = Settings(auth_enabled=False, secret_key=SecretStr(_STRONG))
        assert s.allow_registration is True

    @pytest.mark.unit
    def test_development_auth_on_with_inherited_default_still_boots(self, monkeypatch):
        monkeypatch.setenv("RENFIELD_ENV", "development")
        monkeypatch.delenv("ALLOW_REGISTRATION", raising=False)
        s = Settings(auth_enabled=True, secret_key=SecretStr(_STRONG))
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
