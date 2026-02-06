"""
Tests für Settings API

Testet:
- Wake Word Konfiguration
- Wake Word Service Status
"""

from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

# ============================================================================
# Wake Word Settings Tests
# ============================================================================

class TestWakeWordSettings:
    """Tests für Wake Word Settings"""

    @pytest.mark.integration
    async def test_get_wakeword_settings(self, async_client: AsyncClient):
        """Testet GET /api/settings/wakeword"""
        response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        assert "enabled" in data
        assert "keyword" in data
        assert "threshold" in data
        assert "cooldown_ms" in data
        assert "available_keywords" in data

    @pytest.mark.integration
    async def test_wakeword_settings_has_keywords(self, async_client: AsyncClient):
        """Testet, dass verfügbare Keywords zurückgegeben werden"""
        with patch('api.routes.settings.settings') as mock_settings:
            mock_settings.wake_word_enabled = True
            mock_settings.wake_word_default = "alexa"
            mock_settings.wake_word_threshold = 0.5
            mock_settings.wake_word_cooldown_ms = 2000

            with patch('api.routes.settings._check_server_fallback', return_value=False):
                response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()

        # Check available keywords structure
        keywords = data["available_keywords"]
        assert isinstance(keywords, list)
        assert len(keywords) >= 1

        # Each keyword should have id, label, description
        for keyword in keywords:
            assert "id" in keyword
            assert "label" in keyword
            assert "description" in keyword


# ============================================================================
# Wake Word Status Tests
# ============================================================================

class TestWakeWordStatus:
    """Tests für Wake Word Service Status"""

    @pytest.mark.integration
    async def test_wakeword_status_available(self, async_client: AsyncClient):
        """Testet GET /api/settings/wakeword/status - Service verfügbar"""
        mock_service = MagicMock()
        mock_service.get_status.return_value = {
            "available": True,
            "model_loaded": True,
            "keywords": ["alexa", "hey_jarvis"]
        }

        with patch('services.wakeword_service.get_wakeword_service', return_value=mock_service):
            response = await async_client.get("/api/settings/wakeword/status")

        assert response.status_code == 200
        data = response.json()
        # Response structure depends on actual implementation
        assert "available" in data or "error" in data

    @pytest.mark.integration
    async def test_wakeword_status_unavailable(self, async_client: AsyncClient):
        """Testet GET /api/settings/wakeword/status - Service nicht verfügbar"""
        with patch('services.wakeword_service.get_wakeword_service', side_effect=Exception("OpenWakeWord not installed")):
            response = await async_client.get("/api/settings/wakeword/status")

        assert response.status_code == 200
        data = response.json()
        assert data["available"] is False
        assert "error" in data

    @pytest.mark.integration
    async def test_wakeword_status_not_loaded(self, async_client: AsyncClient):
        """Testet GET /api/settings/wakeword/status - Model nicht geladen"""
        mock_service = MagicMock()
        mock_service.get_status.return_value = {
            "available": True,
            "model_loaded": False,
            "error": "Model not loaded yet"
        }

        with patch('services.wakeword_service.get_wakeword_service', return_value=mock_service):
            response = await async_client.get("/api/settings/wakeword/status")

        assert response.status_code == 200
        data = response.json()
        # Response structure depends on actual implementation
        assert "model_loaded" in data or "available" in data


# ============================================================================
# Server Fallback Tests
# ============================================================================

class TestServerFallback:
    """Tests für Server-side Fallback"""

    @pytest.mark.integration
    async def test_server_fallback_available(self, async_client: AsyncClient):
        """Testet Server-side Fallback verfügbar"""
        with patch('api.routes.settings.settings') as mock_settings:
            mock_settings.wake_word_enabled = True
            mock_settings.wake_word_default = "alexa"
            mock_settings.wake_word_threshold = 0.5
            mock_settings.wake_word_cooldown_ms = 2000

            with patch('api.routes.settings._check_server_fallback', return_value=True):
                response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        assert data["server_fallback_available"] is True

    @pytest.mark.integration
    async def test_server_fallback_unavailable(self, async_client: AsyncClient):
        """Testet Server-side Fallback nicht verfügbar"""
        with patch('api.routes.settings.settings') as mock_settings:
            mock_settings.wake_word_enabled = True
            mock_settings.wake_word_default = "alexa"
            mock_settings.wake_word_threshold = 0.5
            mock_settings.wake_word_cooldown_ms = 2000

            with patch('api.routes.settings._check_server_fallback', return_value=False):
                response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        assert data["server_fallback_available"] is False


# ============================================================================
# Configuration Values Tests
# ============================================================================

class TestConfigurationValues:
    """Tests für Konfigurationswerte"""

    @pytest.mark.integration
    async def test_default_keyword_value(self, async_client: AsyncClient):
        """Testet Default-Keyword Wert"""
        response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        # The default keyword is "alexa" from environment or DB
        assert "keyword" in data
        assert data["keyword"] in ["alexa", "hey_jarvis", "hey_mycroft"]

    @pytest.mark.integration
    async def test_threshold_value(self, async_client: AsyncClient):
        """Testet Threshold Wert"""
        response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        # Threshold should be between 0.1 and 1.0
        assert "threshold" in data
        assert 0.1 <= data["threshold"] <= 1.0

    @pytest.mark.integration
    async def test_cooldown_value(self, async_client: AsyncClient):
        """Testet Cooldown Wert"""
        response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        # Cooldown should be between 500 and 10000
        assert "cooldown_ms" in data
        assert 500 <= data["cooldown_ms"] <= 10000

    @pytest.mark.integration
    async def test_wakeword_enabled_field_exists(self, async_client: AsyncClient):
        """Testet dass enabled-Feld zurückgegeben wird"""
        response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        # enabled should be a boolean
        assert "enabled" in data
        assert isinstance(data["enabled"], bool)
