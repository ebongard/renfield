"""The /ws/satellite handshake credential (auth-on cutover, device half).

Under ``AUTH_ENABLED=true`` the backend rejects a satellite WS handshake that
carries no credential with 403 — before the register frame is ever read. Two
device-side things must hold for an enrolled satellite to get in:

1. it must DERIVE ``sat.<satellite_id>.<enrollment_psk>`` from the PSK it
   already has (the backend recognises a satellite credential only by that
   prefix and verifies the secret against the same bcrypt hash the register
   frame uses), instead of fetching a token from ``POST /api/ws/token``, which
   is 401 for a device on an auth-on instance;
2. it must send it under the header kwarg the INSTALLED websockets version
   accepts — ``extra_headers`` was renamed ``additional_headers`` in 14.0 and
   REJECTED in 16.x, which the Pi images ship. connect() swallows the
   resulting TypeError, so the wrong name looks like "cannot connect" forever.
"""
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from renfield_satellite.config import Config, SatelliteConfig, ServerConfig
from renfield_satellite.network import websocket_client as wsc


class TestHeadersKwarg:
    @pytest.mark.satellite
    def test_matches_installed_websockets_signature(self):
        """The chosen kwarg is one connect() actually accepts."""
        params = inspect.signature(wsc.websockets.connect).parameters
        assert wsc._HEADERS_KWARG in params

    @pytest.mark.satellite
    def test_prefers_modern_name(self):
        def connect(uri, *, additional_headers=None, extra_headers=None):
            ...

        with patch.object(wsc, "websockets", MagicMock(connect=connect)):
            assert wsc._headers_kwarg() == "additional_headers"

    @pytest.mark.satellite
    def test_falls_back_to_legacy_name(self):
        def connect(uri, *, extra_headers=None):
            ...

        with patch.object(wsc, "websockets", MagicMock(connect=connect)):
            assert wsc._headers_kwarg() == "extra_headers"

    @pytest.mark.satellite
    def test_no_websockets_does_not_raise(self):
        with patch.object(wsc, "websockets", None):
            assert wsc._headers_kwarg() == "additional_headers"


class TestAuthorizationHeaderReachesConnect:
    @pytest.mark.satellite
    @pytest.mark.asyncio
    async def test_token_is_sent_under_the_supported_kwarg(self):
        client = wsc.WebSocketClient(
            satellite_id="sat-test", room="TestRoom", server_url="wss://x/ws/satellite"
        )
        client.set_auth_token("sat.sat-test.secret")
        client._register = AsyncMock(return_value=True)  # type: ignore[method-assign]

        captured = {}

        async def fake_connect(uri, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch.object(wsc.websockets, "connect", side_effect=fake_connect):
            await client.connect()
        # connect() spawns the heartbeat/receive loops against the stub socket;
        # stop them before asserting, or they raise into un-awaited tasks and
        # leak into the next test.
        client._running = False
        await client._cancel_background_tasks()

        assert wsc._HEADERS_KWARG in captured
        assert captured[wsc._HEADERS_KWARG] == {
            "Authorization": "Bearer sat.sat-test.secret"
        }
        assert "extra_headers" not in captured or wsc._HEADERS_KWARG == "extra_headers"


def _satellite(server: ServerConfig, sat_id: str = "sat-wohnzimmer"):
    """A Satellite instance with only the fields these tests touch."""
    from renfield_satellite.satellite import Satellite

    sat = Satellite.__new__(Satellite)
    sat.config = Config(satellite=SatelliteConfig(id=sat_id), server=server)
    sat.ws_client = MagicMock()
    return sat


class TestCredentialGate:
    @pytest.mark.satellite
    def test_enrollment_token_alone_arms_the_handshake(self):
        """auth_enabled is False on every device provisioned while auth was off."""
        sat = _satellite(ServerConfig(auth_enabled=False, enrollment_token="psk-abc"))
        assert sat._needs_handshake_credential() is True

    @pytest.mark.satellite
    def test_auth_enabled_alone_still_arms_it(self):
        sat = _satellite(ServerConfig(auth_enabled=True, enrollment_token=None))
        assert sat._needs_handshake_credential() is True

    @pytest.mark.satellite
    def test_neither_stays_off(self):
        sat = _satellite(ServerConfig(auth_enabled=False, enrollment_token=None))
        assert sat._needs_handshake_credential() is False


class TestDerivedCredential:
    @pytest.mark.satellite
    @pytest.mark.asyncio
    async def test_psk_is_prefixed_with_sat_and_the_id(self):
        sat = _satellite(ServerConfig(enrollment_token="psk-abc"))
        with patch("renfield_satellite.satellite.fetch_ws_token") as faucet:
            await sat._fetch_and_set_token("wss://x/ws/satellite")
        sat.ws_client.set_auth_token.assert_called_once_with("sat.sat-wohnzimmer.psk-abc")
        faucet.assert_not_called()

    @pytest.mark.satellite
    @pytest.mark.asyncio
    async def test_preconfigured_auth_token_wins(self):
        sat = _satellite(
            ServerConfig(auth_token="sat.sat-wohnzimmer.explicit", enrollment_token="psk-abc")
        )
        with patch("renfield_satellite.satellite.fetch_ws_token") as faucet:
            await sat._fetch_and_set_token("wss://x/ws/satellite")
        sat.ws_client.set_auth_token.assert_called_once_with("sat.sat-wohnzimmer.explicit")
        faucet.assert_not_called()

    @pytest.mark.satellite
    @pytest.mark.asyncio
    async def test_unenrolled_satellite_still_uses_the_faucet(self):
        """An auth-off instance with FEATURE-level auth: the legacy path stays."""
        sat = _satellite(ServerConfig(auth_enabled=True, enrollment_token=None))
        with patch(
            "renfield_satellite.satellite.fetch_ws_token",
            new=AsyncMock(return_value=("jwt-token", "1.0")),
        ) as faucet:
            await sat._fetch_and_set_token("wss://x/ws/satellite")
        faucet.assert_awaited_once()
        sat.ws_client.set_auth_token.assert_called_once_with("jwt-token")


class TestReconnectGate:
    @pytest.mark.satellite
    @pytest.mark.asyncio
    async def test_reconnect_sets_the_credential_after_a_failed_discovery(self):
        """start() never set one (discovery failed, no URL); the loop must.

        Without this the satellite reconnects forever with no Authorization
        header — 403 under auth-on, which is the outage this module exists for.
        """
        from renfield_satellite.config import Config, SatelliteConfig, ServerConfig
        from renfield_satellite.satellite import Satellite

        sat = Satellite.__new__(Satellite)
        sat.config = Config(
            satellite=SatelliteConfig(id="sat-wohnzimmer"),
            server=ServerConfig(
                url=None, auto_discover=True, auth_enabled=False, enrollment_token="psk-abc"
            ),
        )
        sat.ws_client = MagicMock()
        sat.ws_client.connect = AsyncMock(return_value=True)
        sat._discover_server = AsyncMock(return_value="wss://found/ws/satellite")
        sat._running = True
        sat._updating = False

        with patch("renfield_satellite.satellite.asyncio.sleep", new=AsyncMock()):
            assert await sat._reconnect_with_discovery() is True

        sat.ws_client.set_auth_token.assert_called_once_with("sat.sat-wohnzimmer.psk-abc")


class TestModelDownloaderToken:
    """The PSK is the device's whole identity — it never leaves the handshake."""

    @pytest.mark.satellite
    def test_psk_is_not_handed_to_the_downloader(self):
        sat = _satellite(ServerConfig(enrollment_token="psk-abc"))
        sat.ws_client._auth_token = "sat.sat-wohnzimmer.psk-abc"
        assert sat._model_downloader_token() is None

    @pytest.mark.satellite
    def test_faucet_token_still_passes_through(self):
        sat = _satellite(ServerConfig(auth_enabled=True))
        sat.ws_client._auth_token = "eyJhbGciOi.short.lived"
        assert sat._model_downloader_token() == "eyJhbGciOi.short.lived"

    @pytest.mark.satellite
    def test_no_token_is_no_token(self):
        sat = _satellite(ServerConfig())
        sat.ws_client._auth_token = None
        assert sat._model_downloader_token() is None
