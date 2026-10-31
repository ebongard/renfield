"""The two auth flags have been merged into ONE: `AUTH_ENABLED`.

`WS_AUTH_ENABLED` was never a decision of its own -- the consistency validator
(#697) refused to start on `auth_enabled and not ws_auth_enabled`, so only
both-on and both-off were permitted and the second flag carried no information
whatsoever. It has been removed as a setting in its own right; every read site
reads `settings.auth_enabled`.

For the transition the old env key is still ACCEPTED (both live instances set
it, and their ConfigMaps live in two different repos):

* absent        -> everything is derived from `auth_enabled`
* agreeing      -> ONE deprecation warning, startup as before
* contradicting -> a hard boot error (the same spirit as the old validator:
  better not to start at all than to start with an auth posture the operator
  obviously meant differently)
"""
import pytest
from loguru import logger
from pydantic import SecretStr

from utils.config import Settings

# A strong key, so that fail_closed_on_insecure_jwt_key (which runs first)
# passes and the auth consistency validator is really what gets checked.
_STRONG = "x" * 48


def _capture_warnings():
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    return messages, sink_id


def _settings_with_warnings(**kwargs) -> tuple[Settings, list[str]]:
    messages, sink_id = _capture_warnings()
    try:
        s = Settings(_env_file=None, **kwargs)
    finally:
        logger.remove(sink_id)
    return s, messages


@pytest.mark.backend
@pytest.mark.unit
class TestSingleAuthFlag:
    def test_ws_auth_enabled_is_no_longer_a_setting(self, monkeypatch):
        """The setting no longer exists -- there is only `auth_enabled`."""
        monkeypatch.delenv("WS_AUTH_ENABLED", raising=False)
        s = Settings(_env_file=None)
        assert not hasattr(s, "ws_auth_enabled")

    def test_absent_legacy_key_derives_from_auth_enabled(self, monkeypatch):
        monkeypatch.delenv("WS_AUTH_ENABLED", raising=False)
        off, warnings_off = _settings_with_warnings(auth_enabled=False)
        assert off.auth_enabled is False
        on, warnings_on = _settings_with_warnings(
            auth_enabled=True, secret_key=SecretStr(_STRONG)
        )
        assert on.auth_enabled is True
        # Without the legacy key nothing may sound like a deprecation either.
        assert not any("WS_AUTH_ENABLED" in m for m in warnings_off + warnings_on)


@pytest.mark.backend
@pytest.mark.unit
class TestLegacyWsAuthEnabled:
    @pytest.mark.parametrize("value", ["true", "false"])
    def test_agreeing_legacy_value_warns_but_starts(self, monkeypatch, value):
        monkeypatch.setenv("WS_AUTH_ENABLED", value)
        auth_on = value == "true"
        kwargs = {"auth_enabled": auth_on}
        if auth_on:
            kwargs["secret_key"] = SecretStr(_STRONG)

        s, messages = _settings_with_warnings(**kwargs)

        assert s.auth_enabled is auth_on
        assert any("WS_AUTH_ENABLED" in m for m in messages), (
            "a legacy key that is set must be reported as deprecated exactly once"
        )

    def test_contradicting_legacy_value_refuses_to_start(self, monkeypatch):
        """WS_AUTH_ENABLED=false with AUTH_ENABLED=true -- the old phantom-control case."""
        monkeypatch.setenv("WS_AUTH_ENABLED", "false")
        with pytest.raises(ValueError, match="WS_AUTH_ENABLED"):
            Settings(_env_file=None, auth_enabled=True, secret_key=SecretStr(_STRONG))

    def test_contradicting_legacy_value_other_direction_refuses_to_start(self, monkeypatch):
        """WS_AUTH_ENABLED=true with AUTH_ENABLED=false.

        This used to be permitted (WS authenticated, REST not) -- now it
        contradicts the one decision and is not resolved silently in favour of
        either side.
        """
        monkeypatch.setenv("WS_AUTH_ENABLED", "true")
        with pytest.raises(ValueError, match="WS_AUTH_ENABLED"):
            Settings(_env_file=None, auth_enabled=False)
