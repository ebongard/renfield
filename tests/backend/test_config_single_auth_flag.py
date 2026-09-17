"""Die zwei Auth-Flags sind zu EINEM verschmolzen: `AUTH_ENABLED`.

`WS_AUTH_ENABLED` war nie eine eigene Entscheidung — der Konsistenz-Validator
(#697) verweigerte den Start bei `auth_enabled and not ws_auth_enabled`, also
waren nur beide-an und beide-aus erlaubt und das zweite Flag trug keinerlei
Information. Es ist als eigenständige Einstellung entfernt; jede Lesestelle
liest `settings.auth_enabled`.

Übergangsweise wird der alte Env-Schlüssel noch ANGENOMMEN (beide Live-
Instanzen setzen ihn, ihre ConfigMaps liegen in zwei verschiedenen Repos):

* abwesend        → alles leitet sich aus `auth_enabled` ab
* übereinstimmend → EINE Deprecation-Warnung, Start wie bisher
* widersprüchlich → harter Boot-Fehler (derselbe Geist wie der alte Validator:
  lieber gar nicht starten als mit einer Auth-Posture, die der Operator
  offensichtlich anders gemeint hat)
"""
import pytest
from loguru import logger
from pydantic import SecretStr

from utils.config import Settings

# Starker Key, damit fail_closed_on_insecure_jwt_key (läuft zuerst) passiert und
# wirklich der Auth-Konsistenz-Validator geprüft wird.
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
        """Die Einstellung existiert nicht mehr — es gibt nur noch `auth_enabled`."""
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
        # Ohne den Altschlüssel darf auch nichts nach Deprecation klingen.
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
            "ein gesetzter Altschlüssel muss genau einmal als veraltet gemeldet werden"
        )

    def test_contradicting_legacy_value_refuses_to_start(self, monkeypatch):
        """WS_AUTH_ENABLED=false bei AUTH_ENABLED=true — der alte Phantom-Control-Fall."""
        monkeypatch.setenv("WS_AUTH_ENABLED", "false")
        with pytest.raises(ValueError, match="WS_AUTH_ENABLED"):
            Settings(_env_file=None, auth_enabled=True, secret_key=SecretStr(_STRONG))

    def test_contradicting_legacy_value_other_direction_refuses_to_start(self, monkeypatch):
        """WS_AUTH_ENABLED=true bei AUTH_ENABLED=false.

        Früher war das erlaubt (WS authentifiziert, REST nicht) — jetzt ist es ein
        Widerspruch zur einen Entscheidung und wird nicht stillschweigend
        zugunsten einer der beiden Seiten aufgelöst.
        """
        monkeypatch.setenv("WS_AUTH_ENABLED", "true")
        with pytest.raises(ValueError, match="WS_AUTH_ENABLED"):
            Settings(_env_file=None, auth_enabled=False)
