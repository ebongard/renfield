"""
Tests for API Rate Limiter

Tests cover:
- get_client_ip() extraction from various request scenarios
- rate_limit_exceeded_handler response format
- setup_rate_limiter() configuration
- limit decorator functions
- is_plugin_enabled helper
"""

import importlib
import sys
from unittest.mock import MagicMock, patch

# These heavy/optional dependencies are stubbed ONLY when genuinely not
# importable in the current environment. The previous unconditional
# `sys.modules[mod] = MagicMock()` poisoned the whole session: in the real
# backend test container these packages ARE installed, and replacing them
# with MagicMocks broke every later test that transitively imported them
# (e.g. route modules importing asyncpg) — most visibly causing the
# ha_glue route mounts in conftest to silently fail, 404-ing every
# /api/rooms, /api/camera, /api/homeassistant test. Stub-if-missing keeps
# this file runnable standalone without polluting a real environment.
_optional_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice",
    "speechbrain", "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
    "slowapi", "slowapi.errors", "slowapi.middleware", "slowapi.util",
]
_stubbed: list[str] = []
for _mod in _optional_stubs:
    if _mod in sys.modules:
        continue
    try:
        importlib.import_module(_mod)
    except Exception:  # noqa: BLE001 — genuinely absent: stub it
        sys.modules[_mod] = MagicMock()
        _stubbed.append(_mod)

# slowapi stubs need specific attributes — only apply when slowapi was
# actually stubbed (absent), never overwrite a real install.
if "slowapi" in _stubbed:
    sys.modules["slowapi"].Limiter = MagicMock
    sys.modules["slowapi.errors"].RateLimitExceeded = type(
        "RateLimitExceeded", (Exception,), {}
    )
    sys.modules["slowapi.middleware"].SlowAPIMiddleware = MagicMock
    sys.modules["slowapi.util"].get_remote_address = MagicMock(
        return_value="127.0.0.1"
    )

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

@pytest.fixture
def _trusted(monkeypatch):
    """Set TRUSTED_PROXIES and reset the module-level network cache.

    #693: get_client_ip only honors forwarded headers when the direct peer is a
    configured trusted proxy, and walks the chain right-to-left. These tests
    drive that from a controlled trusted-proxy config.
    """
    import services.api_rate_limiter as arl

    def _set(cidrs: str):
        monkeypatch.setattr(arl.settings, "trusted_proxies", cidrs, raising=False)
        arl._trusted_networks = None  # bust the lazy cache
        return arl

    yield _set
    arl._trusted_networks = None  # restore lazy re-read for other tests


class TestGetClientIp:
    """Tests for the get_client_ip function (#693 right-most-untrusted walk)."""

    @pytest.mark.unit
    def test_no_trusted_proxies_legacy_reads_xff_first(self, _trusted):
        """Empty TRUSTED_PROXIES = legacy backwards-compatible behavior.

        Reads X-Forwarded-For[0] so per-client keying keeps working behind a
        proxy out of the box (flipping to direct-IP would collapse all clients
        into the proxy's single bucket — a cluster-wide DoS). This path is
        spoofable by design; TRUSTED_PROXIES enables the spoof-resistant walk.
        """
        _trusted("")
        request = _make_request(
            headers={"X-Forwarded-For": "203.0.113.50, 10.0.0.1", "X-Real-IP": "198.51.100.1"},
            client_host="10.0.0.1",
        )
        assert get_client_ip(request) == "203.0.113.50"

    @pytest.mark.unit
    def test_no_trusted_proxies_falls_back_to_direct_when_no_headers(self, _trusted):
        """Empty TRUSTED_PROXIES + no forwarded headers → direct socket IP."""
        _trusted("")
        request = _make_request(client_host="192.168.1.42")
        assert get_client_ip(request) == "192.168.1.42"

    @pytest.mark.unit
    def test_untrusted_direct_peer_ignores_forwarded_headers(self, _trusted):
        """A direct peer NOT in TRUSTED_PROXIES → its XFF is untrusted → direct IP."""
        _trusted("172.18.0.0/16")
        request = _make_request(
            headers={"X-Forwarded-For": "203.0.113.50"},
            client_host="8.8.8.8",  # not in the trusted range
        )
        assert get_client_ip(request) == "8.8.8.8"

    @pytest.mark.unit
    def test_trusted_proxy_single_client(self, _trusted):
        """Trusted proxy forwarding one client → that client IP."""
        _trusted("172.18.0.0/16")
        request = _make_request(
            headers={"X-Forwarded-For": "203.0.113.50"},
            client_host="172.18.0.9",
        )
        assert get_client_ip(request) == "203.0.113.50"

    @pytest.mark.unit
    def test_right_most_untrusted_is_returned(self, _trusted):
        """Walk right-to-left: return the right-most address NOT a trusted proxy.

        Chain: realclient, proxy1(trusted), proxy2(trusted). The right-most
        untrusted entry is `realclient`.
        """
        _trusted("172.18.0.0/16")
        request = _make_request(
            headers={"X-Forwarded-For": "203.0.113.50, 172.18.0.7, 172.18.0.8"},
            client_host="172.18.0.9",
        )
        assert get_client_ip(request) == "203.0.113.50"

    @pytest.mark.unit
    def test_spoofed_left_entries_are_ignored(self, _trusted):
        """An attacker prepending fake left-most entries cannot change identity.

        The attacker's real address (right-most, appended by our trusted proxy)
        is what we key on; the injected `1.2.3.4` left entry is ignored.
        """
        _trusted("172.18.0.0/16")
        request = _make_request(
            headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.50"},
            client_host="172.18.0.9",
        )
        assert get_client_ip(request) == "203.0.113.50"

    @pytest.mark.unit
    def test_x_real_ip_used_when_no_xff(self, _trusted):
        """Trusted proxy with only X-Real-IP → that IP."""
        _trusted("172.18.0.0/16")
        request = _make_request(
            headers={"X-Real-IP": "198.51.100.1"},
            client_host="172.18.0.9",
        )
        assert get_client_ip(request) == "198.51.100.1"

    @pytest.mark.unit
    def test_all_hops_trusted_falls_back_to_direct(self, _trusted):
        """If the whole XFF chain is trusted proxies, fall back to the direct IP."""
        _trusted("172.18.0.0/16")
        request = _make_request(
            headers={"X-Forwarded-For": "172.18.0.5, 172.18.0.6"},
            client_host="172.18.0.9",
        )
        assert get_client_ip(request) == "172.18.0.9"


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
