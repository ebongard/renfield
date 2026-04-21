"""
Tests for F5d — TLS certificate fingerprint pinning.

Coverage:
- Fingerprint normalization (`AA:BB:CC` form vs lowercase no-separator)
- HTTP endpoints skip the check entirely
- Pre-flight matches expected fingerprint → True
- Pre-flight observes a different fingerprint → False with the
  observed value returned
- Connection failure (host unreachable) → False with `actual=None`
- Asker integration: when the peer has a fingerprint and verification
  fails, the query yields a final-error WITHOUT touching the
  initiate/retrieve endpoints
"""
from __future__ import annotations

import hashlib
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.federation_cert_pin import (
    _normalize_fingerprint,
    verify_peer_cert_fingerprint,
)


# =============================================================================
# Pure helpers
# =============================================================================


class TestNormalize:
    @pytest.mark.unit
    def test_lowercase(self):
        assert _normalize_fingerprint("ABCDEF") == "abcdef"

    @pytest.mark.unit
    def test_strips_colon_separators(self):
        assert _normalize_fingerprint("AA:BB:CC:DD") == "aabbccdd"

    @pytest.mark.unit
    def test_strips_spaces(self):
        assert _normalize_fingerprint("aa bb cc") == "aabbcc"

    @pytest.mark.unit
    def test_already_normalized(self):
        assert _normalize_fingerprint("aabbcc") == "aabbcc"

    @pytest.mark.unit
    def test_strips_sha256_algorithm_prefix(self):
        """curl / SPKI-style `sha256:AA:BB:...` should normalize to the
        same hex as the bare openssl form."""
        with_prefix = "sha256:AA:BB:CC:DD"
        without = "AA:BB:CC:DD"
        assert _normalize_fingerprint(with_prefix) == _normalize_fingerprint(without)
        assert _normalize_fingerprint(with_prefix) == "aabbccdd"


# =============================================================================
# verify_peer_cert_fingerprint — pure paths
# =============================================================================


class TestVerifyFingerprint:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_http_endpoint_skips_check(self):
        """Non-https endpoints have no cert to pin; return True with
        actual=None so the asker proceeds (Ed25519 sig still secures
        the payload)."""
        ok, actual = await verify_peer_cert_fingerprint(
            "http://192.168.1.5:8080", "deadbeef" * 8,
        )
        assert ok is True
        assert actual is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_malformed_endpoint_fails_closed(self):
        """A URL with no host returns False — better to fail than to
        accidentally bypass pinning on a typo."""
        ok, actual = await verify_peer_cert_fingerprint(
            "https://", "deadbeef" * 8,
        )
        assert ok is False
        assert actual is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_connection_failure_returns_false_unreachable(self, monkeypatch):
        """When the TCP+TLS handshake itself fails (host down, refused
        connection), return (False, None) so the caller treats it as
        a verification failure rather than silently allowing."""
        async def boom(*a, **kw):
            raise OSError("connection refused")
        monkeypatch.setattr("asyncio.open_connection", boom)

        ok, actual = await verify_peer_cert_fingerprint(
            "https://unreachable.example:9999", "ab" * 32,
        )
        assert ok is False
        assert actual is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_matching_fingerprint_returns_true(self, monkeypatch):
        """Happy path — observed cert matches the pin, normalized
        comparison ignores case + colon separators."""
        cert_der = b"fake-cert-bytes"
        expected = hashlib.sha256(cert_der).hexdigest()

        # Stub asyncio.open_connection to return a writer whose
        # ssl_object yields our fake cert.
        ssl_obj = MagicMock()
        ssl_obj.getpeercert.return_value = cert_der
        writer = MagicMock()
        writer.get_extra_info.return_value = ssl_obj
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        async def fake_open(*a, **kw):
            return MagicMock(), writer
        monkeypatch.setattr("asyncio.open_connection", fake_open)

        # Test with mixed-case + colon-separated form on the pin side.
        formatted = ":".join(expected.upper()[i:i+2] for i in range(0, len(expected), 2))
        ok, actual = await verify_peer_cert_fingerprint(
            "https://mom.example:443", formatted,
        )
        assert ok is True
        assert actual == expected.lower()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_mismatched_fingerprint_returns_false_with_actual(self, monkeypatch):
        """When the cert differs, return False AND the actual SHA so
        the caller can log it for forensics."""
        observed_cert = b"attacker-cert-bytes"
        observed_sha = hashlib.sha256(observed_cert).hexdigest()
        expected_sha = "0" * 64  # something else

        ssl_obj = MagicMock()
        ssl_obj.getpeercert.return_value = observed_cert
        writer = MagicMock()
        writer.get_extra_info.return_value = ssl_obj
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        async def fake_open(*a, **kw):
            return MagicMock(), writer
        monkeypatch.setattr("asyncio.open_connection", fake_open)

        ok, actual = await verify_peer_cert_fingerprint(
            "https://attacker.example:443", expected_sha,
        )
        assert ok is False
        assert actual == observed_sha

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_empty_cert_bytes_returns_false(self, monkeypatch):
        """An unusual TLS peer that completes the handshake but doesn't
        actually present a cert (b''). Must fail closed — we can't
        pin nothing."""
        ssl_obj = MagicMock()
        ssl_obj.getpeercert.return_value = b""
        writer = MagicMock()
        writer.get_extra_info.return_value = ssl_obj
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        async def fake_open(*a, **kw):
            return MagicMock(), writer
        monkeypatch.setattr("asyncio.open_connection", fake_open)

        ok, actual = await verify_peer_cert_fingerprint(
            "https://empty-cert.example:443", "ab" * 32,
        )
        assert ok is False
        assert actual is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_ssl_object_returns_false(self, monkeypatch):
        """A connection that succeeded TCP-wise but isn't TLS shouldn't
        accidentally pass — `get_extra_info('ssl_object')` is None."""
        writer = MagicMock()
        writer.get_extra_info.return_value = None
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        async def fake_open(*a, **kw):
            return MagicMock(), writer
        monkeypatch.setattr("asyncio.open_connection", fake_open)

        ok, actual = await verify_peer_cert_fingerprint(
            "https://broken.example:443", "ab" * 32,
        )
        assert ok is False
        assert actual is None


# =============================================================================
# Asker integration — pin mismatch aborts before any federation request
# =============================================================================


class TestAskerCertPinIntegration:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_mismatch_aborts_before_initiate(self, tmp_path, monkeypatch):
        """When the pre-flight pin check fails, FederationQueryAsker
        must NOT POST to /initiate — the only error the caller sees
        is the cert-pin failure."""
        from services.federation_identity import (
            init_federation_identity,
            reset_federation_identity_for_tests,
        )
        from services.federation_query_asker import FederationQueryAsker
        from services.mcp_streaming import ProgressChunk

        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "key")

        peer = SimpleNamespace(
            id=1,
            remote_pubkey="a" * 64,
            remote_display_name="Mom",
            transport_config={
                "endpoint_url": "https://mom-renfield.local:8443",
                "tls_fingerprint": "ab" * 32,
            },
        )

        # Pre-flight verifier returns mismatch.
        async def fake_verify(endpoint, pin, *, timeout=5.0):
            return False, "ff" * 32  # observed something else

        monkeypatch.setattr(
            "services.federation_cert_pin.verify_peer_cert_fingerprint",
            fake_verify,
        )

        # Spy on _initiate — must NOT be called.
        initiate_called = False
        original_initiate = FederationQueryAsker._initiate

        async def spy_initiate(self, client, endpoint, query_text):
            nonlocal initiate_called
            initiate_called = True
            return await original_initiate(self, client, endpoint, query_text)

        monkeypatch.setattr(
            FederationQueryAsker, "_initiate", spy_initiate,
        )

        asker = FederationQueryAsker(client=MagicMock())
        items = []
        async for item in asker.query_peer(peer, "what's for dinner?"):
            items.append(item)

        assert initiate_called is False, (
            "Pin mismatch must short-circuit BEFORE any federation "
            "request — initiate was reached anyway"
        )
        assert len(items) == 1
        # Final-error dict
        assert items[0]["success"] is False
        assert "fingerprint mismatch" in items[0]["message"].lower()

        reset_federation_identity_for_tests()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_pin_skips_check(self, tmp_path, monkeypatch):
        """A peer without `tls_fingerprint` configured does NOT trigger
        the pre-flight probe (would slow every query for nothing)."""
        from services.federation_identity import (
            init_federation_identity,
            reset_federation_identity_for_tests,
        )
        from services.federation_query_asker import FederationQueryAsker

        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "key")

        peer = SimpleNamespace(
            id=1,
            remote_pubkey="a" * 64,
            remote_display_name="Mom",
            transport_config={
                "endpoint_url": "https://mom-renfield.local:8443",
                # no tls_fingerprint
            },
        )

        verify_called = False

        async def fake_verify(*a, **kw):
            nonlocal verify_called
            verify_called = True
            return True, None

        monkeypatch.setattr(
            "services.federation_cert_pin.verify_peer_cert_fingerprint",
            fake_verify,
        )

        # Stub the wire calls so we don't actually hit the network.
        async def fake_initiate(self, client, endpoint, query_text):
            return None  # signals "peer rejected initiate"
        monkeypatch.setattr(
            FederationQueryAsker, "_initiate", fake_initiate,
        )

        asker = FederationQueryAsker(client=MagicMock())
        async for _ in asker.query_peer(peer, "q"):
            pass

        assert verify_called is False, (
            "Cert-pin probe must NOT run for peers without a fingerprint"
        )

        reset_federation_identity_for_tests()
