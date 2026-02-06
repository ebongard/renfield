"""
Tests for User Preferences API Routes

Tests cover:
- GET /api/preferences (all preferences)
- GET /api/preferences/language
- PUT /api/preferences/language
- Auth-dependent behavior (authenticated vs anonymous)
- Language validation
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

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

from api.routes.preferences import router as preferences_router
from services.auth_service import get_current_user, get_optional_user
from services.database import get_db

# =============================================================================
# Helpers
# =============================================================================

def _make_mock_user(preferred_language: str = "de", username: str = "testuser"):
    """Create a mock User with required attributes."""
    user = MagicMock()
    user.preferred_language = preferred_language
    user.username = username
    return user


def _create_test_app(
    user=None,
    optional_user=None,
    db_session=None,
):
    """Create a minimal FastAPI app with the preferences router and overrides."""
    app = FastAPI()
    app.include_router(preferences_router, prefix="/api/preferences")

    if db_session is None:
        db_session = AsyncMock()

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db

    if optional_user is not None:
        app.dependency_overrides[get_optional_user] = lambda: optional_user

    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user

    return app


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_db_session():
    """Mock async database session."""
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


# =============================================================================
# GET /api/preferences/language
# =============================================================================

class TestGetLanguagePreference:
    """Tests for the GET /language endpoint."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_authenticated_user_returns_stored_language(self, mock_db_session):
        """Authenticated user should get their stored preference."""
        user = _make_mock_user(preferred_language="en")
        app = _create_test_app(optional_user=user, db_session=mock_db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/preferences/language")
        assert resp.status_code == 200
        assert resp.json()["language"] == "en"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_anonymous_user_returns_default_language(self, mock_db_session):
        """Anonymous user should get the default language from settings."""
        app = _create_test_app(optional_user=None, db_session=mock_db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/preferences/language")
        assert resp.status_code == 200
        data = resp.json()
        assert "language" in data
        assert len(data["language"]) >= 2


# =============================================================================
# PUT /api/preferences/language
# =============================================================================

class TestSetLanguagePreference:
    """Tests for the PUT /language endpoint."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_set_valid_language(self, mock_db_session):
        """Should update language for authenticated user."""
        user = _make_mock_user(preferred_language="de")

        async def fake_refresh(obj):
            obj.preferred_language = "en"

        mock_db_session.refresh = fake_refresh

        app = _create_test_app(user=user, optional_user=user, db_session=mock_db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch("api.routes.preferences.settings") as mock_settings:
                mock_settings.supported_languages_list = ["de", "en"]
                resp = await client.put(
                    "/api/preferences/language",
                    json={"language": "en"},
                )
        assert resp.status_code == 200
        assert resp.json()["language"] == "en"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_set_unsupported_language_returns_400(self, mock_db_session):
        """Should reject unsupported language codes."""
        user = _make_mock_user()
        app = _create_test_app(user=user, optional_user=user, db_session=mock_db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch("api.routes.preferences.settings") as mock_settings:
                mock_settings.supported_languages_list = ["de", "en"]
                resp = await client.put(
                    "/api/preferences/language",
                    json={"language": "xx"},
                )
        assert resp.status_code == 400
        assert "Unsupported language" in resp.json()["detail"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_unauthenticated_user_returns_401(self, mock_db_session):
        """Should return 401 for unauthenticated users (get_current_user returns None)."""
        app = _create_test_app(user=None, db_session=mock_db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.put(
                "/api/preferences/language",
                json={"language": "en"},
            )
        assert resp.status_code == 401

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_language_too_short_validation(self, mock_db_session):
        """Language code shorter than 2 chars should fail validation."""
        user = _make_mock_user()
        app = _create_test_app(user=user, optional_user=user, db_session=mock_db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.put(
                "/api/preferences/language",
                json={"language": "x"},
            )
        assert resp.status_code == 422  # Pydantic validation

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_language_too_long_validation(self, mock_db_session):
        """Language code longer than 10 chars should fail validation."""
        user = _make_mock_user()
        app = _create_test_app(user=user, optional_user=user, db_session=mock_db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.put(
                "/api/preferences/language",
                json={"language": "x" * 11},
            )
        assert resp.status_code == 422  # Pydantic validation

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_missing_language_field_returns_422(self, mock_db_session):
        """Missing language field should fail validation."""
        user = _make_mock_user()
        app = _create_test_app(user=user, optional_user=user, db_session=mock_db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.put(
                "/api/preferences/language",
                json={},
            )
        assert resp.status_code == 422


# =============================================================================
# GET /api/preferences
# =============================================================================

class TestGetAllPreferences:
    """Tests for the GET /api/preferences endpoint."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_authenticated_returns_user_prefs(self, mock_db_session):
        """Should return user's preferences plus supported languages."""
        user = _make_mock_user(preferred_language="en")
        app = _create_test_app(optional_user=user, db_session=mock_db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch("api.routes.preferences.settings") as mock_settings:
                mock_settings.default_language = "de"
                mock_settings.supported_languages_list = ["de", "en"]
                resp = await client.get("/api/preferences")
        assert resp.status_code == 200
        data = resp.json()
        assert data["language"] == "en"
        assert "supported_languages" in data
        assert isinstance(data["supported_languages"], list)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_anonymous_returns_default_prefs(self, mock_db_session):
        """Anonymous user should get defaults."""
        app = _create_test_app(optional_user=None, db_session=mock_db_session)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch("api.routes.preferences.settings") as mock_settings:
                mock_settings.default_language = "de"
                mock_settings.supported_languages_list = ["de", "en"]
                resp = await client.get("/api/preferences")
        assert resp.status_code == 200
        data = resp.json()
        assert data["language"] == "de"
