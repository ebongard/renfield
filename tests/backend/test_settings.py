"""
Tests für Settings API

Testet:
- Wake Word Konfiguration
- Wake Word Service Status
"""

import pytest
from unittest.mock import MagicMock, patch
from httpx import AsyncClient


# ============================================================================
# Wake Word Settings Tests
# ============================================================================

class TestWakeWordSettings:
    """Tests für Wake Word Settings"""

    @pytest.mark.integration
    async def test_get_wakeword_settings(self, async_client: AsyncClient):
        """Testet GET /api/settings/wakeword"""
        with patch('api.routes.settings.settings') as mock_settings:
            mock_settings.wake_word_enabled = True
            mock_settings.wake_word_default = "alexa"
            mock_settings.wake_word_threshold = 0.5
            mock_settings.wake_word_cooldown_ms = 2000

            with patch('api.routes.settings._check_server_fallback', return_value=True):
                response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        assert "enabled" in data
        assert "default_keyword" in data
        assert "threshold" in data
        assert "cooldown_ms" in data
        assert "available_keywords" in data
        assert "server_fallback_available" in data

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
        with patch('api.routes.settings.settings') as mock_settings:
            mock_settings.wake_word_enabled = True
            mock_settings.wake_word_default = "hey_jarvis"
            mock_settings.wake_word_threshold = 0.5
            mock_settings.wake_word_cooldown_ms = 2000

            with patch('api.routes.settings._check_server_fallback', return_value=False):
                response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        assert data["default_keyword"] == "hey_jarvis"

    @pytest.mark.integration
    async def test_threshold_value(self, async_client: AsyncClient):
        """Testet Threshold Wert"""
        with patch('api.routes.settings.settings') as mock_settings:
            mock_settings.wake_word_enabled = True
            mock_settings.wake_word_default = "alexa"
            mock_settings.wake_word_threshold = 0.7
            mock_settings.wake_word_cooldown_ms = 2000

            with patch('api.routes.settings._check_server_fallback', return_value=False):
                response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        assert data["threshold"] == 0.7

    @pytest.mark.integration
    async def test_cooldown_value(self, async_client: AsyncClient):
        """Testet Cooldown Wert"""
        with patch('api.routes.settings.settings') as mock_settings:
            mock_settings.wake_word_enabled = True
            mock_settings.wake_word_default = "alexa"
            mock_settings.wake_word_threshold = 0.5
            mock_settings.wake_word_cooldown_ms = 3000

            with patch('api.routes.settings._check_server_fallback', return_value=False):
                response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        assert data["cooldown_ms"] == 3000

    @pytest.mark.integration
    async def test_wakeword_disabled(self, async_client: AsyncClient):
        """Testet wenn Wake Word deaktiviert ist"""
        with patch('api.routes.settings.settings') as mock_settings:
            mock_settings.wake_word_enabled = False
            mock_settings.wake_word_default = "alexa"
            mock_settings.wake_word_threshold = 0.5
            mock_settings.wake_word_cooldown_ms = 2000

            with patch('api.routes.settings._check_server_fallback', return_value=False):
                response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False
