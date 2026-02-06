"""
Tests für Integration Clients

Testet:
- HomeAssistantClient
- FrigateClient
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ============================================================================
# HomeAssistantClient Tests
# ============================================================================

class TestHomeAssistantClient:
    """Tests für HomeAssistantClient"""

    @pytest.fixture
    def ha_client(self):
        """Create HomeAssistantClient with test settings"""
        with patch('integrations.homeassistant.settings') as mock_settings:
            mock_settings.home_assistant_url = "http://ha.local:8123"
            mock_settings.home_assistant_token = "test_token"

            from integrations.homeassistant import HomeAssistantClient
            return HomeAssistantClient()

    @pytest.mark.unit
    def test_client_initialization(self, ha_client):
        """Test: Client wird korrekt initialisiert"""
        assert ha_client.base_url == "http://ha.local:8123"
        assert "Bearer test_token" in ha_client.headers["Authorization"]

    @pytest.mark.unit
    async def test_get_states(self, ha_client):
        """Test: get_states ruft API korrekt auf"""
        mock_response = [
            {"entity_id": "light.wohnzimmer", "state": "on"},
            {"entity_id": "switch.tv", "state": "off"}
        ]

        with patch.object(httpx.AsyncClient, 'get', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: mock_response
            )
            mock_get.return_value.raise_for_status = MagicMock()

            result = await ha_client.get_states()

            assert len(result) == 2
            assert result[0]["entity_id"] == "light.wohnzimmer"

    @pytest.mark.unit
    async def test_get_states_error_handling(self, ha_client):
        """Test: get_states behandelt Fehler"""
        with patch.object(httpx.AsyncClient, 'get', new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = httpx.ConnectError("Connection failed")

            result = await ha_client.get_states()

            assert result == []

    @pytest.mark.unit
    async def test_get_state(self, ha_client):
        """Test: get_state für einzelne Entity"""
        mock_response = {
            "entity_id": "light.wohnzimmer",
            "state": "on",
            "attributes": {"brightness": 255}
        }

        with patch.object(httpx.AsyncClient, 'get', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: mock_response
            )
            mock_get.return_value.raise_for_status = MagicMock()

            result = await ha_client.get_state("light.wohnzimmer")

            assert result["state"] == "on"
            assert result["attributes"]["brightness"] == 255

    @pytest.mark.unit
    async def test_call_service(self, ha_client):
        """Test: call_service sendet korrekten Request"""
        with patch.object(httpx.AsyncClient, 'post', new_callable=AsyncMock) as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            mock_post.return_value.raise_for_status = MagicMock()

            result = await ha_client.call_service(
                "light",
                "turn_on",
                "light.wohnzimmer",
                {"brightness": 128}
            )

            assert result is True
            mock_post.assert_called_once()

            # Check the call arguments
            call_args = mock_post.call_args
            assert "light/turn_on" in call_args[0][0]
            assert call_args[1]["json"]["entity_id"] == "light.wohnzimmer"
            assert call_args[1]["json"]["brightness"] == 128

    @pytest.mark.unit
    async def test_turn_on(self, ha_client):
        """Test: turn_on leitet an call_service weiter"""
        with patch.object(ha_client, 'call_service', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = True

            result = await ha_client.turn_on("light.test")

            assert result is True
            mock_call.assert_called_once_with("light", "turn_on", "light.test")

    @pytest.mark.unit
    async def test_turn_off(self, ha_client):
        """Test: turn_off leitet an call_service weiter"""
        with patch.object(ha_client, 'call_service', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = True

            result = await ha_client.turn_off("switch.fernseher")

            assert result is True
            mock_call.assert_called_once_with("switch", "turn_off", "switch.fernseher")

    @pytest.mark.unit
    async def test_toggle(self, ha_client):
        """Test: toggle leitet an call_service weiter"""
        with patch.object(ha_client, 'call_service', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = True

            result = await ha_client.toggle("light.flur")

            assert result is True
            mock_call.assert_called_once_with("light", "toggle", "light.flur")

    @pytest.mark.unit
    async def test_set_value_brightness(self, ha_client):
        """Test: set_value für Helligkeit"""
        with patch.object(ha_client, 'call_service', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = True

            result = await ha_client.set_value(
                "light.wohnzimmer",
                128,
                "brightness"
            )

            assert result is True
            mock_call.assert_called_once_with(
                "light",
                "turn_on",
                "light.wohnzimmer",
                {"brightness": 128}
            )

    @pytest.mark.unit
    async def test_set_value_temperature(self, ha_client):
        """Test: set_value für Temperatur"""
        with patch.object(ha_client, 'call_service', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = True

            result = await ha_client.set_value(
                "climate.heizung",
                21.5,
                "temperature"
            )

            assert result is True
            mock_call.assert_called_once_with(
                "climate",
                "set_temperature",
                "climate.heizung",
                {"temperature": 21.5}
            )

    @pytest.mark.unit
    async def test_search_entities(self, ha_client):
        """Test: search_entities filtert korrekt"""
        mock_states = [
            {"entity_id": "light.wohnzimmer", "attributes": {"friendly_name": "Wohnzimmer Licht"}},
            {"entity_id": "light.kueche", "attributes": {"friendly_name": "Küchen Licht"}},
            {"entity_id": "switch.tv", "attributes": {"friendly_name": "Fernseher"}},
        ]

        with patch.object(ha_client, 'get_states', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_states

            # Search for "wohnzimmer"
            results = await ha_client.search_entities("wohnzimmer")

            assert len(results) == 1
            assert results[0]["entity_id"] == "light.wohnzimmer"

    @pytest.mark.unit
    async def test_is_configured(self, ha_client):
        """Test: is_configured prüft URL und Token"""
        assert ha_client.base_url is not None
        assert ha_client.token is not None


# ============================================================================
# FrigateClient Tests
# ============================================================================

class TestFrigateClient:
    """Tests für FrigateClient"""

    @pytest.fixture
    def frigate_client(self):
        """Create FrigateClient with test settings"""
        with patch('integrations.frigate.settings') as mock_settings:
            mock_settings.frigate_url = "http://frigate.local:5000"

            from integrations.frigate import FrigateClient
            return FrigateClient()

    @pytest.mark.unit
    def test_client_initialization(self, frigate_client):
        """Test: Client wird korrekt initialisiert"""
        assert frigate_client.base_url == "http://frigate.local:5000"

    @pytest.mark.unit
    async def test_get_events(self, frigate_client):
        """Test: get_events ruft API korrekt auf"""
        mock_events = [
            {"id": "event1", "camera": "front_door", "label": "person"},
            {"id": "event2", "camera": "garage", "label": "car"},
        ]

        with patch.object(httpx.AsyncClient, 'get', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: mock_events
            )
            mock_get.return_value.raise_for_status = MagicMock()

            result = await frigate_client.get_events()

            assert len(result) == 2
            assert result[0]["label"] == "person"

    @pytest.mark.unit
    async def test_get_events_with_filter(self, frigate_client):
        """Test: get_events mit Camera Filter"""
        mock_events = [
            {"id": "event1", "camera": "front_door", "label": "person"}
        ]

        with patch.object(httpx.AsyncClient, 'get', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: mock_events
            )
            mock_get.return_value.raise_for_status = MagicMock()

            result = await frigate_client.get_events(camera="front_door")

            mock_get.assert_called_once()
            call_args = mock_get.call_args
            assert "camera=front_door" in call_args[0][0] or "front_door" in str(call_args)

    @pytest.mark.unit
    async def test_get_events_error_handling(self, frigate_client):
        """Test: get_events behandelt Fehler"""
        with patch.object(httpx.AsyncClient, 'get', new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = httpx.ConnectError("Connection failed")

            result = await frigate_client.get_events()

            assert result == []

    @pytest.mark.unit
    async def test_get_snapshot(self, frigate_client):
        """Test: get_snapshot lädt Bild"""
        fake_image = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        with patch.object(httpx.AsyncClient, 'get', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                content=fake_image
            )
            mock_get.return_value.raise_for_status = MagicMock()

            result = await frigate_client.get_snapshot("event-123")

            assert result == fake_image
            assert b"PNG" in result


# ============================================================================
# Integration Client Edge Cases
# ============================================================================

class TestIntegrationEdgeCases:
    """Tests für Edge Cases der Integration Clients"""

    @pytest.mark.unit
    async def test_ha_client_not_configured(self):
        """Test: HA Client ohne Konfiguration"""
        with patch('integrations.homeassistant.settings') as mock_settings:
            mock_settings.home_assistant_url = None
            mock_settings.home_assistant_token = None

            from integrations.homeassistant import HomeAssistantClient
            client = HomeAssistantClient()

            # Should return empty/False without throwing
            result = await client.get_states()
            assert result == []

    @pytest.mark.unit
    async def test_ha_client_timeout(self):
        """Test: HA Client Timeout"""
        with patch('integrations.homeassistant.settings') as mock_settings:
            mock_settings.home_assistant_url = "http://slow.server"
            mock_settings.home_assistant_token = "token"

            from integrations.homeassistant import HomeAssistantClient
            client = HomeAssistantClient()

            with patch.object(httpx.AsyncClient, 'get', new_callable=AsyncMock) as mock_get:
                mock_get.side_effect = httpx.TimeoutException("Timeout")

                result = await client.get_states()
                assert result == []

    @pytest.mark.unit
    async def test_frigate_client_not_configured(self):
        """Test: Frigate Client ohne Konfiguration"""
        with patch('integrations.frigate.settings') as mock_settings:
            mock_settings.frigate_url = None

            from integrations.frigate import FrigateClient
            client = FrigateClient()

            result = await client.get_events()
            assert result == []

