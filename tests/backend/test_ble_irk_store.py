"""
Tests for the per-person BLE IRK store: encryption-at-rest, the presence
service IRK helpers, and identity-based presence routing.
"""
import pytest

from services.secret_encryption import decrypt_secret, encrypt_secret, InvalidToken

IRK_HEX = "3a66fe43118690229991659536ef9a4b"


def _svc():
    """A fresh PresenceService without running __init__ (mirrors the suite's
    existing fixture style), with the IRK caches populated."""
    from ha_glue.services.presence_service import PresenceService
    svc = PresenceService.__new__(PresenceService)
    svc._mac_to_user = {}
    svc._mac_to_method = {}
    svc._presence = {}
    svc._sightings = {}
    svc._hysteresis_threshold = 2
    svc._stale_timeout = 120.0
    svc._rssi_threshold = -80
    svc._filter_enabled = False
    svc._filter_alpha_up = 0.5
    svc._filter_alpha_down = 0.1
    svc._filter_fresh_seconds = 35.0
    svc._switch_enter_margin_db = 8.0
    svc._room_names = {}
    svc._user_names = {}
    svc._user_first_names = {}
    svc._user_last_names = {}
    svc._pending_events = []
    svc._irk_label_to_user = {}
    svc._irks_hex = {}
    return svc


@pytest.mark.backend
@pytest.mark.unit
class TestSecretEncryption:
    def test_roundtrip(self):
        token = encrypt_secret(IRK_HEX)
        assert token != IRK_HEX          # not stored in plaintext
        assert decrypt_secret(token) == IRK_HEX

    def test_tampered_token_raises(self):
        with pytest.raises(InvalidToken):
            decrypt_secret("not-a-valid-fernet-token")


@pytest.mark.backend
@pytest.mark.unit
class TestPresenceIRKHelpers:
    def test_get_ble_irks_shape(self):
        svc = _svc()
        svc._irks_hex = {"alice-iphone": IRK_HEX}
        assert svc.get_ble_irks() == [{"name": "alice-iphone", "irk": IRK_HEX}]

    def test_user_for_key_resolves_irk_and_mac(self):
        svc = _svc()
        svc._irk_label_to_user = {"alice-iphone": 7}
        svc._mac_to_user = {"AA:BB:CC:DD:EE:FF": 3}
        assert svc._user_for_key("irk:alice-iphone") == 7
        assert svc._user_for_key("irk:unknown") is None
        assert svc._user_for_key("AA:BB:CC:DD:EE:FF") == 3
        assert svc._user_for_key("00:00:00:00:00:00") is None

    @pytest.mark.asyncio
    async def test_process_ble_report_routes_identity_to_user(self):
        svc = _svc()
        svc._irk_label_to_user = {"alice-iphone": 7}
        # A rotating RPA the satellite resolved to "alice-iphone"
        await svc.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=5,
            devices=[{"mac": "7E:3C:54:25:17:3E", "rssi": -50, "identity": "alice-iphone"}],
            room_name="Kitchen",
        )
        # Tracked under the stable identity key, not the rotating MAC
        assert "irk:alice-iphone" in svc._sightings
        assert "7E:3C:54:25:17:3E" not in svc._sightings

    @pytest.mark.asyncio
    async def test_process_ble_report_ignores_unknown_identity(self):
        svc = _svc()  # no IRKs registered
        await svc.process_ble_report(
            satellite_id="sat-kitchen",
            room_id=5,
            devices=[{"mac": "7E:3C:54:25:17:3E", "rssi": -50, "identity": "ghost"}],
            room_name="Kitchen",
        )
        assert svc._sightings == {}

    @pytest.mark.asyncio
    async def test_load_irks_skips_undecryptable_rows(self):
        """A row that fails to decrypt (e.g. SECRET_KEY changed) is skipped while
        good rows still load — the decrypt-skip silent-failure branch."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock, patch

        svc = _svc()
        good = SimpleNamespace(label="alice-iphone", irk_encrypted="good-token", user_id=7, is_enabled=True)
        bad = SimpleNamespace(label="bob-iphone", irk_encrypted="stale-token", user_id=8, is_enabled=True)
        result = MagicMock()
        result.scalars.return_value.all.return_value = [good, bad]
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)

        def fake_decrypt(token):
            if token == "good-token":
                return IRK_HEX
            raise InvalidToken()

        with patch("services.secret_encryption.decrypt_secret", side_effect=fake_decrypt):
            await svc._load_irks(db)

        assert svc._irks_hex == {"alice-iphone": IRK_HEX}
        assert svc._irk_label_to_user == {"alice-iphone": 7}
