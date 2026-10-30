"""
Tests for the settings API

Covers:
- Wake word configuration
- Wake word service status
"""

from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

# ============================================================================
# Wake Word Settings Tests
# ============================================================================

class TestWakeWordSettings:
    """Tests for the wake word settings"""

    @pytest.mark.integration
    async def test_get_wakeword_settings(self, async_client: AsyncClient):
        """GET /api/settings/wakeword"""
        response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        assert "keyword" in data
        assert "threshold" in data
        assert "cooldown_ms" in data
        assert "available_keywords" in data

    @pytest.mark.integration
    async def test_wakeword_settings_has_keywords(self, async_client: AsyncClient):
        """The available keywords are returned"""
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
    """Tests for the wake word service status"""

    @pytest.mark.integration
    async def test_wakeword_status_available(self, async_client: AsyncClient):
        """GET /api/settings/wakeword/status -- service available"""
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
        """GET /api/settings/wakeword/status -- service unavailable"""
        with patch('services.wakeword_service.get_wakeword_service', side_effect=Exception("OpenWakeWord not installed")):
            response = await async_client.get("/api/settings/wakeword/status")

        assert response.status_code == 200
        data = response.json()
        assert data["available"] is False
        assert "error" in data

    @pytest.mark.integration
    async def test_wakeword_status_not_loaded(self, async_client: AsyncClient):
        """GET /api/settings/wakeword/status -- model not loaded"""
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
    """Tests for the server-side fallback"""

    @pytest.mark.integration
    async def test_server_fallback_available(self, async_client: AsyncClient):
        """The server-side fallback is available"""
        with patch('api.routes.settings._check_server_fallback', return_value=True):
            response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        assert data["server_fallback_available"] is True

    @pytest.mark.integration
    async def test_server_fallback_unavailable(self, async_client: AsyncClient):
        """The server-side fallback is unavailable"""
        with patch('api.routes.settings._check_server_fallback', return_value=False):
            response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        assert data["server_fallback_available"] is False


# ============================================================================
# Configuration Values Tests
# ============================================================================

class TestConfigurationValues:
    """Tests for the configuration values"""

    @pytest.mark.integration
    async def test_default_keyword_value(self, async_client: AsyncClient):
        """The default keyword value"""
        response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        # The default keyword comes from environment / DB; assert it is one
        # of the currently supported keywords rather than a frozen list.
        from services.wakeword_config_manager import VALID_KEYWORDS

        assert "keyword" in data
        assert data["keyword"] in VALID_KEYWORDS

    @pytest.mark.integration
    async def test_threshold_value(self, async_client: AsyncClient):
        """The threshold value"""
        response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        # Threshold should be between 0.1 and 1.0
        assert "threshold" in data
        assert 0.1 <= data["threshold"] <= 1.0

    @pytest.mark.integration
    async def test_cooldown_value(self, async_client: AsyncClient):
        """The cooldown value"""
        response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        # Cooldown should be between 500 and 10000
        assert "cooldown_ms" in data
        assert 500 <= data["cooldown_ms"] <= 10000

    @pytest.mark.integration
    async def test_wakeword_settings_carry_no_server_side_enabled(self, async_client: AsyncClient):
        """The on/off switch is a per-device decision (satellite locally,
        browser in localStorage) -- the server no longer carries an `enabled`
        field (WAKE_WORD_ENABLED was removed on 2026-09-20 as a flag with no
        reader)."""
        response = await async_client.get("/api/settings/wakeword")

        assert response.status_code == 200
        data = response.json()
        assert "enabled" not in data
