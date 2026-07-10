"""Security review M1 — fail closed on the default JWT signing key.

With AUTH_ENABLED=true and the public placeholder SECRET_KEY, anyone can forge
an admin JWT. The Settings validator must refuse to start in that config, while
leaving AUTH_ENABLED=false (single-user/household) and dev/test unaffected.
"""
import pytest
from pydantic import SecretStr

from utils.config import Settings

_PLACEHOLDER = Settings.model_fields["secret_key"].default
if isinstance(_PLACEHOLDER, SecretStr):
    _PLACEHOLDER = _PLACEHOLDER.get_secret_value()
_STRONG = "x" * 48


@pytest.mark.backend
@pytest.mark.unit
class TestSecretKeyFailClosed:
    def test_auth_on_with_placeholder_key_raises(self):
        with pytest.raises(ValueError):
            Settings(auth_enabled=True, secret_key=SecretStr(_PLACEHOLDER))

    def test_auth_on_with_strong_key_ok(self):
        # ws_auth_enabled=True is now required alongside auth_enabled (#697
        # assert_auth_config_consistency); a strong key + coherent auth config
        # must still construct cleanly.
        s = Settings(
            auth_enabled=True, ws_auth_enabled=True, secret_key=SecretStr(_STRONG)
        )
        assert s.secret_key.get_secret_value() == _STRONG

    def test_auth_off_with_placeholder_key_ok(self):
        # household/single-user mode (dev env) tolerates the default (no JWT trust)
        s = Settings(auth_enabled=False, secret_key=SecretStr(_PLACEHOLDER))
        assert s.auth_enabled is False

    # --- #692: production trigger (independent of auth) + entropy/length check ---

    def test_production_env_with_placeholder_key_raises(self, monkeypatch):
        """RENFIELD_ENV=production arms the guard even with auth off (#692)."""
        monkeypatch.setenv("RENFIELD_ENV", "production")
        with pytest.raises(ValueError):
            Settings(auth_enabled=False, secret_key=SecretStr(_PLACEHOLDER))

    def test_production_env_with_short_key_raises(self, monkeypatch):
        monkeypatch.setenv("RENFIELD_ENV", "production")
        with pytest.raises(ValueError):
            Settings(auth_enabled=False, secret_key=SecretStr("too-short"))

    def test_auth_on_with_short_key_raises(self):
        """A non-placeholder but weak (<32) key is rejected when auth is on."""
        with pytest.raises(ValueError):
            Settings(auth_enabled=True, secret_key=SecretStr("short"))

    def test_production_env_with_strong_key_ok(self, monkeypatch):
        monkeypatch.setenv("RENFIELD_ENV", "production")
        s = Settings(auth_enabled=False, secret_key=SecretStr(_STRONG))
        assert s.secret_key.get_secret_value() == _STRONG

    def test_dev_env_short_key_ok(self):
        """Dev/test with auth off is never blocked (no regression)."""
        s = Settings(auth_enabled=False, secret_key=SecretStr("short"))
        assert s.auth_enabled is False
