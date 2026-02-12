"""
Tests for Intent Registry API Routes

Tests cover:
- GET /api/intents/status
- GET /api/intents/prompt
- GET /api/intents/check/{intent_name}
- GET /api/intents/integrations/summary
"""

import sys
from unittest.mock import MagicMock

_missing_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice",
    "speechbrain", "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
    "redis", "redis.asyncio",
    "slowapi", "slowapi.errors", "slowapi.middleware", "slowapi.util",
    "jose", "passlib", "passlib.context",
]
for _mod in _missing_stubs:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.routes.intents import router as intents_router

# =============================================================================
# Helpers
# =============================================================================

def _create_test_app():
    """Create a minimal FastAPI app with the intents router."""
    app = FastAPI()
    app.include_router(intents_router, prefix="/api/intents")
    return app


@pytest.fixture
async def intents_client():
    """Async HTTP client for intents API tests — no auth required."""
    app = _create_test_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# =============================================================================
# GET /api/intents/status
# =============================================================================

class TestIntentStatus:
    """Tests for the /status endpoint."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_status_returns_200(self, intents_client):
        """Endpoint should return 200."""
        resp = await intents_client.get("/api/intents/status")
        assert resp.status_code == 200

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_status_contains_expected_fields(self, intents_client):
        """Response should contain all expected top-level fields."""
        resp = await intents_client.get("/api/intents/status")
        data = resp.json()
        assert "total_intents" in data
        assert "enabled_integrations" in data
        assert "disabled_integrations" in data
        assert "integrations" in data
        assert "mcp_tools" in data

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_status_integrations_structure(self, intents_client):
        """Each integration should have name, title, enabled, intents."""
        resp = await intents_client.get("/api/intents/status")
        data = resp.json()
        for integration in data["integrations"]:
            assert "name" in integration
            assert "title" in integration
            assert "enabled" in integration
            assert "intent_count" in integration
            assert "intents" in integration

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_status_lang_en(self, intents_client):
        """Should accept lang=en query parameter."""
        resp = await intents_client.get("/api/intents/status?lang=en")
        assert resp.status_code == 200
        data = resp.json()
        # The general integration is always enabled
        general = next(
            (i for i in data["integrations"] if i["name"] == "general"),
            None,
        )
        assert general is not None
        assert general["enabled"] is True


# =============================================================================
# GET /api/intents/prompt
# =============================================================================

class TestIntentPrompt:
    """Tests for the /prompt endpoint."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_prompt_returns_200(self, intents_client):
        """Endpoint should return 200."""
        resp = await intents_client.get("/api/intents/prompt")
        assert resp.status_code == 200

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_prompt_contains_expected_fields(self, intents_client):
        """Response should contain language, intent_types, examples."""
        resp = await intents_client.get("/api/intents/prompt")
        data = resp.json()
        assert "language" in data
        assert "intent_types" in data
        assert "examples" in data

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_prompt_default_language_is_de(self, intents_client):
        """Default language should be 'de'."""
        resp = await intents_client.get("/api/intents/prompt")
        assert resp.json()["language"] == "de"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_prompt_lang_en(self, intents_client):
        """Should return English prompt when lang=en."""
        resp = await intents_client.get("/api/intents/prompt?lang=en")
        assert resp.json()["language"] == "en"


# =============================================================================
# GET /api/intents/check/{intent_name}
# =============================================================================

class TestCheckIntent:
    """Tests for the /check/{intent_name} endpoint."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_check_existing_intent(self, intents_client):
        """Should return available=True for a known intent."""
        resp = await intents_client.get("/api/intents/check/general.conversation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "general.conversation"
        assert data["available"] is True
        assert data["definition"] is not None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_check_nonexistent_intent(self, intents_client):
        """Should return available=False for an unknown intent."""
        resp = await intents_client.get("/api/intents/check/nonexistent.intent")
        assert resp.status_code == 200
        data = resp.json()
        assert data["intent"] == "nonexistent.intent"
        assert data["available"] is False
        assert data["definition"] is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_check_intent_definition_structure(self, intents_client):
        """Known intent definition should have name, description_de, description_en, parameters."""
        resp = await intents_client.get("/api/intents/check/general.conversation")
        definition = resp.json()["definition"]
        assert "name" in definition
        assert "description_de" in definition
        assert "description_en" in definition
        assert "parameters" in definition


# =============================================================================
# GET /api/intents/integrations/summary
# =============================================================================

class TestIntegrationsSummary:
    """Tests for the /integrations/summary endpoint."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_summary_returns_200(self, intents_client):
        """Should return 200."""
        resp = await intents_client.get("/api/intents/integrations/summary")
        assert resp.status_code == 200

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_summary_structure(self, intents_client):
        """Should have enabled, disabled, mcp_enabled."""
        resp = await intents_client.get("/api/intents/integrations/summary")
        data = resp.json()
        assert "enabled" in data
        assert "disabled" in data
        assert "mcp_enabled" in data
        assert isinstance(data["enabled"], list)
        assert isinstance(data["disabled"], list)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_summary_general_is_always_enabled(self, intents_client):
        """The 'general' integration should always appear in enabled list."""
        resp = await intents_client.get("/api/intents/integrations/summary")
        data = resp.json()
        enabled_names = [e["name"] for e in data["enabled"]]
        assert "general" in enabled_names
