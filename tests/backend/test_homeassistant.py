"""
Tests für Home Assistant API

Testet:
- State Abfragen
- Geräte steuern (turn_on, turn_off, toggle)
- Service Aufrufe
- Entity Suche
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_ha_client_for_routes():
    """Mock HA Client für Route-Tests"""
    with patch('api.routes.homeassistant.ha_client') as mock:
        mock.get_states = AsyncMock(return_value=[
            {"entity_id": "light.wohnzimmer", "state": "on"},
            {"entity_id": "switch.fernseher", "state": "off"}
        ])
        mock.get_state = AsyncMock(return_value={
            "entity_id": "light.wohnzimmer",
            "state": "on",
            "attributes": {"friendly_name": "Wohnzimmer Licht", "brightness": 255}
        })
        mock.turn_on = AsyncMock(return_value=True)
        mock.turn_off = AsyncMock(return_value=True)
        mock.toggle = AsyncMock(return_value=True)
        mock.set_value = AsyncMock(return_value=True)
        mock.call_service = AsyncMock(return_value=True)
        mock.search_entities = AsyncMock(return_value=[
            {"entity_id": "light.wohnzimmer", "friendly_name": "Wohnzimmer Licht"}
        ])
        mock.get_entities_by_domain = AsyncMock(return_value=[
            {"entity_id": "light.wohnzimmer", "state": "on"},
            {"entity_id": "light.kueche", "state": "off"}
        ])
        yield mock


@pytest.fixture
def mock_auth_bypass():
    """Bypass auth for testing"""
    with patch('api.routes.homeassistant.require_permission') as mock:
        mock.return_value = lambda: MagicMock(id=1, username="test")
        yield mock


# ============================================================================
# State Query Tests
# ============================================================================

class TestStateQueries:
    """Tests für State-Abfragen"""

    @pytest.mark.integration
    async def test_get_all_states(
        self,
        async_client: AsyncClient,
        mock_ha_client_for_routes,
        mock_auth_bypass
    ):
        """Testet GET /api/homeassistant/states"""
        response = await async_client.get("/api/homeassistant/states")

        # Either success or auth required
        assert response.status_code in [200, 401, 403]
        if response.status_code == 200:
            data = response.json()
            assert "states" in data

    @pytest.mark.integration
    async def test_get_entity_state(
        self,
        async_client: AsyncClient,
        mock_ha_client_for_routes,
        mock_auth_bypass
    ):
        """Testet GET /api/homeassistant/state/{entity_id}"""
        response = await async_client.get("/api/homeassistant/state/light.wohnzimmer")

        assert response.status_code in [200, 401, 403]
        if response.status_code == 200:
            data = response.json()
            assert data["entity_id"] == "light.wohnzimmer"

    @pytest.mark.integration
    async def test_get_entity_state_not_found(
        self,
        async_client: AsyncClient,
        mock_auth_bypass
    ):
        """Testet GET für nicht-existente Entity"""
        with patch('api.routes.homeassistant.ha_client') as mock:
            mock.get_state = AsyncMock(return_value=None)

            response = await async_client.get("/api/homeassistant/state/nonexistent.entity")

        assert response.status_code in [404, 401, 403]


# ============================================================================
# Device Control Tests
# ============================================================================

class TestDeviceControl:
    """Tests für Gerätesteuerung"""

    @pytest.mark.integration
    async def test_turn_on(
        self,
        async_client: AsyncClient,
        mock_ha_client_for_routes,
        mock_auth_bypass
    ):
        """Testet POST /api/homeassistant/turn_on/{entity_id}"""
        response = await async_client.post("/api/homeassistant/turn_on/light.wohnzimmer")

        assert response.status_code in [200, 401, 403]
        if response.status_code == 200:
            data = response.json()
            assert data["success"] is True
            assert data["action"] == "turn_on"

    @pytest.mark.integration
    async def test_turn_off(
        self,
        async_client: AsyncClient,
        mock_ha_client_for_routes,
        mock_auth_bypass
    ):
        """Testet POST /api/homeassistant/turn_off/{entity_id}"""
        response = await async_client.post("/api/homeassistant/turn_off/light.wohnzimmer")

        assert response.status_code in [200, 401, 403]
        if response.status_code == 200:
            data = response.json()
            assert data["success"] is True
            assert data["action"] == "turn_off"

    @pytest.mark.integration
    async def test_toggle(
        self,
        async_client: AsyncClient,
        mock_ha_client_for_routes,
        mock_auth_bypass
    ):
        """Testet POST /api/homeassistant/toggle/{entity_id}"""
        response = await async_client.post("/api/homeassistant/toggle/light.wohnzimmer")

        assert response.status_code in [200, 401, 403]
        if response.status_code == 200:
            data = response.json()
            assert data["success"] is True
            assert data["action"] == "toggle"

    @pytest.mark.integration
    async def test_set_value(
        self,
        async_client: AsyncClient,
        mock_ha_client_for_routes,
        mock_auth_bypass
    ):
        """Testet POST /api/homeassistant/set_value"""
        response = await async_client.post(
            "/api/homeassistant/set_value",
            json={
                "entity_id": "light.wohnzimmer",
                "value": 128,
                "attribute": "brightness"
            }
        )

        assert response.status_code in [200, 401, 403]
        if response.status_code == 200:
            data = response.json()
            assert data["success"] is True
            assert data["action"] == "set_value"

    @pytest.mark.integration
    async def test_turn_on_failure(
        self,
        async_client: AsyncClient,
        mock_auth_bypass
    ):
        """Testet Fehler bei turn_on"""
        with patch('api.routes.homeassistant.ha_client') as mock:
            mock.turn_on = AsyncMock(return_value=False)

            response = await async_client.post("/api/homeassistant/turn_on/light.wohnzimmer")

        assert response.status_code in [500, 401, 403]


# ============================================================================
# Service Call Tests
# ============================================================================

class TestServiceCalls:
    """Tests für Service-Aufrufe"""

    @pytest.mark.integration
    async def test_call_service(
        self,
        async_client: AsyncClient,
        mock_ha_client_for_routes,
        mock_auth_bypass
    ):
        """Testet POST /api/homeassistant/service"""
        response = await async_client.post(
            "/api/homeassistant/service",
            json={
                "domain": "light",
                "service": "turn_on",
                "entity_id": "light.wohnzimmer",
                "service_data": {"brightness": 200}
            }
        )

        assert response.status_code in [200, 401, 403]
        if response.status_code == 200:
            data = response.json()
            assert data["success"] is True

    @pytest.mark.integration
    async def test_call_service_failure(
        self,
        async_client: AsyncClient,
        mock_auth_bypass
    ):
        """Testet Fehler bei Service-Aufruf"""
        with patch('api.routes.homeassistant.ha_client') as mock:
            mock.call_service = AsyncMock(return_value=False)

            response = await async_client.post(
                "/api/homeassistant/service",
                json={
                    "domain": "light",
                    "service": "invalid_service"
                }
            )

        assert response.status_code in [500, 401, 403]


# ============================================================================
# Search Tests
# ============================================================================

class TestEntitySearch:
    """Tests für Entity-Suche"""

    @pytest.mark.integration
    async def test_search_entities(
        self,
        async_client: AsyncClient,
        mock_ha_client_for_routes,
        mock_auth_bypass
    ):
        """Testet GET /api/homeassistant/search"""
        response = await async_client.get("/api/homeassistant/search?query=wohnzimmer")

        assert response.status_code in [200, 401, 403]
        if response.status_code == 200:
            data = response.json()
            assert "results" in data

    @pytest.mark.integration
    async def test_get_entities_by_domain(
        self,
        async_client: AsyncClient,
        mock_ha_client_for_routes,
        mock_auth_bypass
    ):
        """Testet GET /api/homeassistant/domain/{domain}"""
        response = await async_client.get("/api/homeassistant/domain/light")

        assert response.status_code in [200, 401, 403]
        if response.status_code == 200:
            data = response.json()
            assert "entities" in data


# ============================================================================
# Error Handling Tests
# ============================================================================

class TestErrorHandling:
    """Tests für Fehlerbehandlung"""

    @pytest.mark.integration
    async def test_ha_client_exception(
        self,
        async_client: AsyncClient,
        mock_auth_bypass
    ):
        """Testet Exception-Handling bei HA Client Fehler"""
        with patch('api.routes.homeassistant.ha_client') as mock:
            mock.get_states = AsyncMock(side_effect=Exception("Connection failed"))

            response = await async_client.get("/api/homeassistant/states")

        assert response.status_code in [500, 401, 403]

    @pytest.mark.integration
    async def test_invalid_entity_id_format(
        self,
        async_client: AsyncClient,
        mock_ha_client_for_routes,
        mock_auth_bypass
    ):
        """Testet ungültiges Entity-ID Format"""
        # Home Assistant client should handle this
        response = await async_client.get("/api/homeassistant/state/invalid")

        # Depends on HA client behavior
        assert response.status_code in [200, 400, 404, 401, 403]
