"""
Tests for API Rate Limiter

Tests cover:
- get_client_ip() extraction from various request scenarios
- rate_limit_exceeded_handler response format
- setup_rate_limiter() configuration
- limit decorator functions
- is_plugin_enabled helper
"""

import sys
from unittest.mock import MagicMock, patch

_missing_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice",
    "speechbrain", "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
    "slowapi", "slowapi.errors", "slowapi.middleware", "slowapi.util",
]
for _mod in _missing_stubs:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# slowapi stubs need specific attributes
sys.modules["slowapi"].Limiter = MagicMock
sys.modules["slowapi.errors"].RateLimitExceeded = type("RateLimitExceeded", (Exception,), {})
sys.modules["slowapi.middleware"].SlowAPIMiddleware = MagicMock
sys.modules["slowapi.util"].get_remote_address = MagicMock(return_value="127.0.0.1")

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from services.api_rate_limiter import (
    get_client_ip,
    limit_custom,
    rate_limit_exceeded_handler,
    setup_rate_limiter,
)

# =============================================================================
# Helpers
# =============================================================================

def _make_request(headers: dict | None = None, client_host: str = "10.0.0.1") -> MagicMock:
    """Create a mock Request with optional headers and client info."""
    req = MagicMock(spec=Request)
    req.headers = headers or {}
    req.url = MagicMock()
    req.url.path = "/api/test"
    # slowapi's get_remote_address reads request.client.host
    req.client = MagicMock()
    req.client.host = client_host
    return req


# =============================================================================
# get_client_ip tests
# =============================================================================

class TestGetClientIp:
    """Tests for the get_client_ip function."""

    @pytest.mark.unit
    def test_x_forwarded_for_single_ip(self):
        """Should extract IP from X-Forwarded-For header."""
        request = _make_request(headers={"X-Forwarded-For": "203.0.113.50"})
        assert get_client_ip(request) == "203.0.113.50"

    @pytest.mark.unit
    def test_x_forwarded_for_multiple_ips(self):
        """Should take first IP from X-Forwarded-For chain."""
        request = _make_request(
            headers={"X-Forwarded-For": "203.0.113.50, 70.41.3.18, 150.172.238.178"}
        )
        assert get_client_ip(request) == "203.0.113.50"

    @pytest.mark.unit
    def test_x_forwarded_for_with_spaces(self):
        """Should strip whitespace from X-Forwarded-For value."""
        request = _make_request(headers={"X-Forwarded-For": "  203.0.113.50 , 10.0.0.1"})
        assert get_client_ip(request) == "203.0.113.50"

    @pytest.mark.unit
    def test_x_real_ip_header(self):
        """Should use X-Real-IP when X-Forwarded-For is absent."""
        request = _make_request(headers={"X-Real-IP": "198.51.100.1"})
        assert get_client_ip(request) == "198.51.100.1"

    @pytest.mark.unit
    def test_x_forwarded_for_takes_priority_over_x_real_ip(self):
        """X-Forwarded-For should have priority over X-Real-IP."""
        request = _make_request(headers={
            "X-Forwarded-For": "203.0.113.50",
            "X-Real-IP": "198.51.100.1",
        })
        assert get_client_ip(request) == "203.0.113.50"

    @pytest.mark.unit
    def test_fallback_to_remote_address(self):
        """Should fall back to direct client IP when no proxy headers."""
        request = _make_request(client_host="192.168.1.42")
        with patch("services.api_rate_limiter.get_remote_address", return_value="192.168.1.42"):
            ip = get_client_ip(request)
        assert ip == "192.168.1.42"

    @pytest.mark.unit
    def test_empty_forwarded_for_uses_real_ip(self):
        """Empty X-Forwarded-For should fall through to X-Real-IP."""
        # An empty string is falsy, so it should skip to X-Real-IP
        request = _make_request(headers={"X-Forwarded-For": "", "X-Real-IP": "10.20.30.40"})
        assert get_client_ip(request) == "10.20.30.40"


# =============================================================================
# rate_limit_exceeded_handler tests
# =============================================================================

class TestRateLimitExceededHandler:
    """Tests for the rate limit exceeded handler."""

    @pytest.mark.unit
    def test_returns_429_status(self):
        """Handler should return 429 status code."""
        request = _make_request(client_host="10.0.0.1")
        exc = MagicMock()
        exc.retry_after = 30
        exc.detail = "5 per minute"

        with patch("services.api_rate_limiter.get_client_ip", return_value="10.0.0.1"):
            response = rate_limit_exceeded_handler(request, exc)

        assert isinstance(response, JSONResponse)
        assert response.status_code == 429

    @pytest.mark.unit
    def test_response_body_contains_error_fields(self):
        """Response body should contain error, message, retry_after, detail."""
        request = _make_request()
        exc = MagicMock()
        exc.retry_after = 60
        exc.detail = "100 per minute"

        with patch("services.api_rate_limiter.get_client_ip", return_value="127.0.0.1"):
            response = rate_limit_exceeded_handler(request, exc)

        assert response.body is not None
        import json
        body = json.loads(response.body)
        assert body["error"] == "rate_limit_exceeded"
        assert body["retry_after"] == 60
        assert "60" in body["message"]
        assert body["detail"] == "100 per minute"

    @pytest.mark.unit
    def test_retry_after_header_set(self):
        """Response should include Retry-After header."""
        request = _make_request()
        exc = MagicMock()
        exc.retry_after = 45
        exc.detail = "10 per minute"

        with patch("services.api_rate_limiter.get_client_ip", return_value="127.0.0.1"):
            response = rate_limit_exceeded_handler(request, exc)

        assert response.headers.get("retry-after") == "45"

    @pytest.mark.unit
    def test_handler_defaults_retry_after_when_missing(self):
        """Should default retry_after to 60 when exc has no retry_after."""
        request = _make_request()
        exc = MagicMock(spec=[])  # no attributes at all

        with patch("services.api_rate_limiter.get_client_ip", return_value="127.0.0.1"):
            response = rate_limit_exceeded_handler(request, exc)

        import json
        body = json.loads(response.body)
        assert body["retry_after"] == 60


# =============================================================================
# setup_rate_limiter tests
# =============================================================================

class TestSetupRateLimiter:
    """Tests for the setup_rate_limiter function."""

    @pytest.mark.unit
    def test_disabled_does_not_add_middleware(self):
        """When rate limiting is disabled, no middleware should be added."""
        app = MagicMock(spec=FastAPI)
        with patch("services.api_rate_limiter.settings") as mock_settings:
            mock_settings.api_rate_limit_enabled = False
            setup_rate_limiter(app)

        app.add_middleware.assert_not_called()
        app.add_exception_handler.assert_not_called()

    @pytest.mark.unit
    def test_enabled_adds_middleware_and_handler(self):
        """When enabled, should add middleware and exception handler."""
        app = MagicMock(spec=FastAPI)
        app.state = MagicMock()
        with patch("services.api_rate_limiter.settings") as mock_settings:
            mock_settings.api_rate_limit_enabled = True
            mock_settings.api_rate_limit_default = "100/minute"
            setup_rate_limiter(app)

        app.add_middleware.assert_called_once()
        app.add_exception_handler.assert_called_once()

    @pytest.mark.unit
    def test_enabled_sets_limiter_on_app_state(self):
        """When enabled, limiter should be set on app.state."""
        app = MagicMock(spec=FastAPI)
        app.state = MagicMock()
        with patch("services.api_rate_limiter.settings") as mock_settings:
            mock_settings.api_rate_limit_enabled = True
            mock_settings.api_rate_limit_default = "100/minute"
            setup_rate_limiter(app)

        assert app.state.limiter is not None


# =============================================================================
# Decorator tests
# =============================================================================

class TestLimitDecorators:
    """Tests for the rate limit decorator functions."""

    @pytest.mark.unit
    def test_limit_custom_returns_decorator(self):
        """limit_custom should return a callable decorator."""
        decorator = limit_custom("5/minute")
        assert callable(decorator)
