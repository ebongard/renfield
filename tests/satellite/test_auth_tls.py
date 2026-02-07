"""
Authentication TLS Context Tests

Tests for SSL/TLS context creation in the satellite network auth module.
Verifies correct behavior for HTTPS (with and without verification) and HTTP URLs.
"""

import ssl

import pytest

from renfield_satellite.network.auth import _create_ssl_context_for_url


class TestCreateSslContextForUrl:
    """Tests for _create_ssl_context_for_url()"""

    @pytest.mark.satellite
    def test_https_verify_true_returns_ssl_context(self):
        """Test: HTTPS with verify=True returns SSLContext with certificate verification"""
        ctx = _create_ssl_context_for_url("https://example.com", verify=True)

        assert ctx is not None
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode != ssl.CERT_NONE

    @pytest.mark.satellite
    def test_https_verify_false_disables_cert_check(self):
        """Test: HTTPS with verify=False returns SSLContext with CERT_NONE"""
        ctx = _create_ssl_context_for_url("https://example.com", verify=False)

        assert ctx is not None
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode == ssl.CERT_NONE
        assert ctx.check_hostname is False

    @pytest.mark.satellite
    def test_http_verify_true_returns_none(self):
        """Test: HTTP URL with verify=True returns None (no TLS needed)"""
        ctx = _create_ssl_context_for_url("http://example.com", verify=True)

        assert ctx is None

    @pytest.mark.satellite
    def test_http_default_verify_returns_none(self):
        """Test: HTTP URL with default verify (True) returns None"""
        ctx = _create_ssl_context_for_url("http://example.com")

        assert ctx is None

    @pytest.mark.satellite
    def test_https_verify_true_enables_hostname_check(self):
        """Test: HTTPS with verify=True has check_hostname enabled"""
        ctx = _create_ssl_context_for_url("https://example.com", verify=True)

        assert ctx is not None
        assert ctx.check_hostname is True

    @pytest.mark.satellite
    def test_http_verify_false_still_returns_none(self):
        """Test: HTTP URL with verify=False still returns None (no TLS for HTTP)"""
        ctx = _create_ssl_context_for_url("http://example.com", verify=False)

        assert ctx is None
