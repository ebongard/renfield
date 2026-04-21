"""
Tests for F5e — TOFU auto-pinning during federation pairing.

Coverage:
- `probe_peer_cert_fingerprint` returns hex SHA on success, None on
  every failure mode (non-https, connection error, no SSL, no cert).
- `_with_tofu_fingerprint` helper extracts first HTTPS endpoint, probes,
  latches the SHA into transport_config. Skips gracefully when:
    - endpoints list is empty
    - no HTTPS endpoint (http-only or no URL fields)
    - probe returns None (host unreachable, plain TCP, etc.)
- `_first_https_url` tolerates the three endpoint shapes in the wild
  (bare string, `{"url": ...}`, `{"endpoint_url": ...}`).
- Integration: PairingService.accept_offer + complete_handshake
  populate `tls_fingerprint` via the probe when a HTTPS endpoint is
  advertised; leave it out when the probe fails.
"""
from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.federation_cert_pin import probe_peer_cert_fingerprint
from services.pairing_service import _first_https_url, _with_tofu_fingerprint


# =============================================================================
# _first_https_url — endpoint-shape tolerance
# =============================================================================


class TestFirstHttpsUrl:
    @pytest.mark.unit
    def test_empty_list_returns_none(self):
        assert _first_https_url([]) is None
        assert _first_https_url(None) is None

    @pytest.mark.unit
    def test_bare_string_endpoint(self):
        assert _first_https_url(["https://mom.local:8443"]) == "https://mom.local:8443"

    @pytest.mark.unit
    def test_dict_url_key(self):
        assert _first_https_url([{"url": "https://mom.local:8443"}]) == "https://mom.local:8443"

    @pytest.mark.unit
    def test_dict_endpoint_url_key(self):
        assert _first_https_url([{"endpoint_url": "https://mom.local:8443"}]) == "https://mom.local:8443"

    @pytest.mark.unit
    def test_skips_http_picks_first_https(self):
        """Mixed list — http:// URLs don't qualify for pinning, fall through."""
        eps = [
            {"url": "http://mom.local:8080"},
            {"url": "https://mom.local:8443"},
        ]
        assert _first_https_url(eps) == "https://mom.local:8443"

    @pytest.mark.unit
    def test_no_https_returns_none(self):
        """All http:// — nothing to pin."""
        assert _first_https_url([{"url": "http://mom.local:8080"}]) is None

    @pytest.mark.unit
    def test_skips_malformed_entries(self):
        """Non-string/dict entries are ignored (forward-compat)."""
        eps = [42, None, {"url": "https://mom.local:8443"}]
        assert _first_https_url(eps) == "https://mom.local:8443"

    @pytest.mark.unit
    def test_uppercase_scheme_accepted(self):
        """Scheme match is case-insensitive — `HTTPS://` is valid."""
        assert _first_https_url(["HTTPS://mom.local:8443"]) == "HTTPS://mom.local:8443"

    @pytest.mark.unit
    def test_picks_first_of_multiple_https(self):
        """When several HTTPS endpoints are advertised, we pin the
        first one. Future enhancement: rank by reachability."""
        eps = [
            {"url": "https://a.local:8443"},
            {"url": "https://b.local:8443"},
        ]
        assert _first_https_url(eps) == "https://a.local:8443"


# =============================================================================
# probe_peer_cert_fingerprint
# =============================================================================


class TestProbeFingerprint:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_http_endpoint_returns_none(self):
        assert await probe_peer_cert_fingerprint("http://mom.local:8080") is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_malformed_url_returns_none(self):
        assert await probe_peer_cert_fingerprint("https://") is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_connection_failure_returns_none(self, monkeypatch):
        async def boom(*a, **kw):
            raise OSError("refused")
        monkeypatch.setattr("asyncio.open_connection", boom)
        assert await probe_peer_cert_fingerprint("https://unreachable:9999") is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_success_returns_lowercase_hex(self, monkeypatch):
        cert_der = b"some-peer-cert"
        expected = hashlib.sha256(cert_der).hexdigest()

        ssl_obj = MagicMock()
        ssl_obj.getpeercert.return_value = cert_der
        writer = MagicMock()
        writer.get_extra_info.return_value = ssl_obj
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        async def fake_open(*a, **kw):
            return MagicMock(), writer
        monkeypatch.setattr("asyncio.open_connection", fake_open)

        result = await probe_peer_cert_fingerprint("https://mom.local:8443")
        assert result == expected.lower()


# =============================================================================
# _with_tofu_fingerprint — the pairing-service helper
# =============================================================================


class TestWithTofuFingerprint:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_empty_endpoints_returns_bare_config_and_warns(self, monkeypatch):
        """No endpoints → no pin, no probe attempt. Warning logged so
        operators can distinguish 'probe failed' from 'peer never
        advertised an endpoint' — both block federation queries but
        the fix is different. Loguru doesn't feed pytest's caplog, so
        we intercept the logger directly."""
        warnings_seen: list[str] = []

        def fake_warning(msg, *a, **kw):
            warnings_seen.append(str(msg))

        import services.pairing_service as ps_mod
        monkeypatch.setattr(ps_mod.logger, "warning", fake_warning)

        config = await _with_tofu_fingerprint([])
        assert config == {"endpoints": []}
        assert "tls_fingerprint" not in config
        combined = " ".join(w.lower() for w in warnings_seen)
        assert "endpoint" in combined

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_none_endpoints_returns_bare_config(self):
        """None (missing field) is treated as empty — same outcome,
        same warning, no crash."""
        config = await _with_tofu_fingerprint(None)
        assert config == {"endpoints": []}

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_http_only_endpoints_skip_pin(self):
        """Plain HTTP deploys can't be pinned — config omits
        tls_fingerprint and the probe is never called."""
        eps = [{"url": "http://mom.local:8080"}]
        config = await _with_tofu_fingerprint(eps)
        assert config == {"endpoints": eps}
        assert "tls_fingerprint" not in config

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_https_with_successful_probe_latches_fingerprint(self, monkeypatch):
        """Happy path — cert probe returns a SHA, helper writes it
        through to transport_config."""
        async def fake_probe(url, *, timeout=5.0):
            return "a" * 64
        monkeypatch.setattr(
            "services.pairing_service.probe_peer_cert_fingerprint",
            fake_probe,
        )

        eps = [{"url": "https://mom.local:8443"}]
        config = await _with_tofu_fingerprint(eps)
        assert config["endpoints"] == eps
        assert config["tls_fingerprint"] == "a" * 64

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_https_probe_failure_skips_pin(self, monkeypatch):
        """Probe failure (None) → skip pinning but keep endpoints.
        Logs a warning — operator sees the gap and can fix it
        manually or re-pair."""
        async def fake_probe(url, *, timeout=5.0):
            return None
        monkeypatch.setattr(
            "services.pairing_service.probe_peer_cert_fingerprint",
            fake_probe,
        )

        eps = [{"url": "https://mom.local:8443"}]
        config = await _with_tofu_fingerprint(eps)
        assert config == {"endpoints": eps}
        assert "tls_fingerprint" not in config


# =============================================================================
# PairingService integration — TOFU writes through on both sides
# =============================================================================


class TestPairingServiceTofuIntegration:
    """Both accept_offer (responder side) and complete_handshake
    (initiator side) must auto-pin at pair time. We verify by spying
    on `_upsert_peer_user` and checking the transport_config it
    receives."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_accept_offer_pins_initiator(self, tmp_path, monkeypatch):
        import secrets
        import time

        from services.federation_identity import (
            FederationIdentity,
            init_federation_identity,
            reset_federation_identity_for_tests,
        )
        from services.pairing_service import (
            PairingOffer,
            PairingService,
            _canonical_bytes,
        )

        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "key")
        from cryptography.hazmat.primitives.asymmetric import ed25519
        initiator = FederationIdentity(ed25519.Ed25519PrivateKey.generate())

        async def fake_probe(url, *, timeout=5.0):
            return "b" * 64
        monkeypatch.setattr(
            "services.pairing_service.probe_peer_cert_fingerprint",
            fake_probe,
        )

        captured_transport = {}

        async def spy_upsert(self, **kwargs):
            captured_transport["config"] = kwargs["transport_config"]
            return MagicMock(id=1)

        monkeypatch.setattr(
            PairingService, "_upsert_peer_user", spy_upsert,
        )
        # Stub the circle + membership writes — not what this test cares about.
        monkeypatch.setattr(
            "services.pairing_service._get_or_create_circle",
            AsyncMock(),
        )
        monkeypatch.setattr(
            PairingService, "_upsert_circle_membership", AsyncMock(),
        )

        # Build a valid signed offer.
        now = int(time.time())
        unsigned = {
            "version": 1,
            "initiator_pubkey": initiator.public_key_hex(),
            "initiator_user_id": 42,
            "display_name": "A",
            "nonce": secrets.token_hex(16),
            "issued_at": now,
            "expires_at": now + 600,
            "offered_endpoints": [{"url": "https://a.local:8443"}],
        }
        sig = initiator.sign(_canonical_bytes(unsigned)).hex()
        offer = PairingOffer(**unsigned, signature=sig)

        # Cache the nonce so _verify_offer accepts it.
        from services.pairing_service import _cache_nonce
        _cache_nonce(offer.nonce, offer.expires_at, offer.initiator_user_id)

        svc = PairingService(db=MagicMock())
        current_user = MagicMock(id=99, username="B")
        await svc.accept_offer(
            current_user=current_user,
            offer=offer,
            my_tier_for_you=2,
            accepted_endpoints=[],
        )

        assert "tls_fingerprint" in captured_transport["config"]
        assert captured_transport["config"]["tls_fingerprint"] == "b" * 64
        assert captured_transport["config"]["endpoints"] == [
            {"url": "https://a.local:8443"}
        ]

        reset_federation_identity_for_tests()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_complete_handshake_pins_responder(self, tmp_path, monkeypatch):
        import time

        from services.federation_identity import (
            FederationIdentity,
            init_federation_identity,
            reset_federation_identity_for_tests,
        )
        from services.pairing_service import (
            PairingResponse,
            PairingService,
            _canonical_bytes,
            _cache_nonce,
        )

        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "key")

        # Responder identity signs the response.
        from cryptography.hazmat.primitives.asymmetric import ed25519
        responder = FederationIdentity(ed25519.Ed25519PrivateKey.generate())

        async def fake_probe(url, *, timeout=5.0):
            return "c" * 64
        monkeypatch.setattr(
            "services.pairing_service.probe_peer_cert_fingerprint",
            fake_probe,
        )

        captured_transport = {}

        async def spy_upsert(self, **kwargs):
            captured_transport["config"] = kwargs["transport_config"]
            return MagicMock(id=2)

        monkeypatch.setattr(
            PairingService, "_upsert_peer_user", spy_upsert,
        )
        monkeypatch.setattr(
            "services.pairing_service._get_or_create_circle", AsyncMock(),
        )
        monkeypatch.setattr(
            PairingService, "_upsert_circle_membership", AsyncMock(),
        )

        nonce = "deadbeefdeadbeef" * 2
        _cache_nonce(nonce, int(time.time()) + 600, initiator_user_id=7)
        unsigned = {
            "version": 1,
            "nonce": nonce,
            "responder_pubkey": responder.public_key_hex(),
            "responder_user_id": 33,
            "responder_display_name": "Mom",
            "accepted_endpoints": [{"endpoint_url": "https://mom.local:8443"}],
            "my_tier_for_you": 2,
            "accepted_at": int(time.time()),
        }
        sig = responder.sign(_canonical_bytes(unsigned)).hex()
        response = PairingResponse(**unsigned, signature=sig)

        svc = PairingService(db=MagicMock())
        await svc.complete_handshake(
            current_user=MagicMock(id=7),
            response=response,
            their_tier_for_me=2,
        )

        assert captured_transport["config"]["tls_fingerprint"] == "c" * 64
        assert captured_transport["config"]["endpoints"] == [
            {"endpoint_url": "https://mom.local:8443"}
        ]

        reset_federation_identity_for_tests()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_complete_handshake_without_https_endpoint_skips_pin(
        self, tmp_path, monkeypatch,
    ):
        """When the responder advertises only http:// endpoints, pairing
        still succeeds but the transport_config omits tls_fingerprint."""
        import time

        from services.federation_identity import (
            FederationIdentity,
            init_federation_identity,
            reset_federation_identity_for_tests,
        )
        from services.pairing_service import (
            PairingResponse,
            PairingService,
            _canonical_bytes,
            _cache_nonce,
        )

        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "key")
        from cryptography.hazmat.primitives.asymmetric import ed25519
        responder = FederationIdentity(ed25519.Ed25519PrivateKey.generate())

        probe_called = False

        async def fake_probe(url, *, timeout=5.0):
            nonlocal probe_called
            probe_called = True
            return "deadbeef"
        monkeypatch.setattr(
            "services.pairing_service.probe_peer_cert_fingerprint",
            fake_probe,
        )

        captured = {}

        async def spy_upsert(self, **kwargs):
            captured["config"] = kwargs["transport_config"]
            return MagicMock(id=9)

        monkeypatch.setattr(PairingService, "_upsert_peer_user", spy_upsert)
        monkeypatch.setattr(
            "services.pairing_service._get_or_create_circle", AsyncMock(),
        )
        monkeypatch.setattr(
            PairingService, "_upsert_circle_membership", AsyncMock(),
        )

        nonce = "cafebabecafebabe" * 2
        _cache_nonce(nonce, int(time.time()) + 600, initiator_user_id=1)
        unsigned = {
            "version": 1,
            "nonce": nonce,
            "responder_pubkey": responder.public_key_hex(),
            "responder_user_id": 2,
            "responder_display_name": "Dad",
            "accepted_endpoints": [{"url": "http://dad.local:8080"}],
            "my_tier_for_you": 2,
            "accepted_at": int(time.time()),
        }
        sig = responder.sign(_canonical_bytes(unsigned)).hex()
        response = PairingResponse(**unsigned, signature=sig)

        svc = PairingService(db=MagicMock())
        await svc.complete_handshake(
            current_user=MagicMock(id=1),
            response=response,
            their_tier_for_me=2,
        )

        assert probe_called is False, (
            "Probe must NOT run when no HTTPS endpoint is advertised — "
            "saves a 5s wait on every http-only pairing"
        )
        assert "tls_fingerprint" not in captured["config"]

        reset_federation_identity_for_tests()
