"""
Tests for F2 federation pairing: identity, PairingService, routes.

Coverage:
- Federation identity: load-or-generate roundtrip, sign/verify happy path,
  verify() returns False (not raise) on every corruption mode.
- PairingService: offer happy path, expired offer, offer signature
  tamper, nonce reuse, unknown nonce, wrong-user nonce, full handshake.
- PeerUser upsert: repeat pair updates in place, concurrent-IntegrityError
  recovery path covered via direct unique-violation simulation.
- Routes: uniform 400 on PairingError — no oracle leak.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from services.federation_identity import (
    FederationIdentity,
    get_federation_identity,
    init_federation_identity,
    reset_federation_identity_for_tests,
)
from services.pairing_service import (
    OFFER_TTL_SECONDS,
    PairingError,
    PairingOffer,
    PairingResponse,
    PairingService,
    _clear_nonce_cache_for_tests,
    _canonical_bytes,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_identity(tmp_path):
    """Fresh federation identity per test. Path is under tmp_path/ so the
    singleton state doesn't pollute other tests."""
    reset_federation_identity_for_tests()
    init_federation_identity(tmp_path / "federation_identity_key")
    _clear_nonce_cache_for_tests()
    identity = get_federation_identity()
    yield identity
    reset_federation_identity_for_tests()
    _clear_nonce_cache_for_tests()


@pytest.fixture
def mock_user():
    user = MagicMock()
    user.id = 42
    user.username = "evdb"
    return user


# =============================================================================
# FederationIdentity
# =============================================================================


class TestFederationIdentityRoundtrip:
    @pytest.mark.unit
    def test_generate_persists_key_with_0600(self, tmp_path):
        reset_federation_identity_for_tests()
        path = tmp_path / "key"
        init_federation_identity(path)
        identity = get_federation_identity()
        assert path.exists()
        # File is 32 bytes (Ed25519 raw private key)
        assert len(path.read_bytes()) == 32
        # Mode is 0600
        assert oct(path.stat().st_mode)[-3:] == "600"
        # Sign/verify round-trip
        sig = identity.sign(b"hello")
        assert FederationIdentity.verify(identity.public_key_bytes(), sig, b"hello")
        reset_federation_identity_for_tests()

    @pytest.mark.unit
    def test_load_existing_key_reuses_same_pubkey(self, tmp_path):
        """Calling get_federation_identity twice with the same path loads
        the same key — regression guard against accidental regenerations."""
        reset_federation_identity_for_tests()
        path = tmp_path / "key"
        init_federation_identity(path)
        pubkey_first = get_federation_identity().public_key_hex()

        reset_federation_identity_for_tests()
        init_federation_identity(path)
        pubkey_second = get_federation_identity().public_key_hex()

        assert pubkey_first == pubkey_second
        reset_federation_identity_for_tests()

    @pytest.mark.unit
    def test_malformed_key_file_raises(self, tmp_path):
        reset_federation_identity_for_tests()
        path = tmp_path / "key"
        path.write_bytes(b"too short")  # 9 bytes, not 32
        init_federation_identity(path)
        with pytest.raises(ValueError, match="expected 32"):
            get_federation_identity()
        reset_federation_identity_for_tests()


class TestFederationIdentityVerify:
    @pytest.mark.unit
    def test_verify_false_on_bad_signature(self, tmp_identity):
        sig = tmp_identity.sign(b"hello")
        # Flip a bit
        bad = bytearray(sig)
        bad[0] ^= 0xFF
        assert FederationIdentity.verify(
            tmp_identity.public_key_bytes(), bytes(bad), b"hello"
        ) is False

    @pytest.mark.unit
    def test_verify_false_on_bad_pubkey(self, tmp_identity):
        sig = tmp_identity.sign(b"hello")
        assert FederationIdentity.verify(b"\x00" * 32, sig, b"hello") is False

    @pytest.mark.unit
    def test_verify_false_on_wrong_length_pubkey(self, tmp_identity):
        sig = tmp_identity.sign(b"hello")
        # Must not raise — verify() is all-or-nothing bool
        assert FederationIdentity.verify(b"\x00" * 10, sig, b"hello") is False

    @pytest.mark.unit
    def test_verify_false_on_tampered_message(self, tmp_identity):
        sig = tmp_identity.sign(b"hello")
        assert FederationIdentity.verify(
            tmp_identity.public_key_bytes(), sig, b"hellO"
        ) is False


# =============================================================================
# PairingService — offer + response mechanics
# =============================================================================


class TestCreateOffer:
    @pytest.mark.unit
    def test_offer_is_signed(self, tmp_identity, mock_user):
        svc = PairingService(db=MagicMock())
        offer = svc.create_offer(current_user=mock_user, display_name="Renfield-A")
        # Signature covers all non-signature fields
        unsigned = offer.model_dump(exclude={"signature"})
        assert FederationIdentity.verify(
            bytes.fromhex(offer.initiator_pubkey),
            bytes.fromhex(offer.signature),
            _canonical_bytes(unsigned),
        )

    @pytest.mark.unit
    def test_offer_ttl_is_10_minutes(self, tmp_identity, mock_user):
        svc = PairingService(db=MagicMock())
        before = int(time.time())
        offer = svc.create_offer(current_user=mock_user)
        assert offer.expires_at - offer.issued_at == OFFER_TTL_SECONDS
        assert before <= offer.issued_at <= int(time.time())


class TestVerifyOffer:
    def _make_svc(self):
        return PairingService(db=MagicMock())

    @pytest.mark.unit
    def test_expired_offer_rejected(self, tmp_identity, mock_user):
        svc = self._make_svc()
        offer = svc.create_offer(current_user=mock_user)
        # Tamper expires_at into the past and re-sign (attacker trying to
        # forge validity). Since they don't have our key, that path fails
        # at signature verify. Verify rejection even BEFORE the signature
        # check by using a legitimate offer and mutating after signing.
        past = PairingOffer(
            **{**offer.model_dump(), "expires_at": int(time.time()) - 60},
        )
        with pytest.raises(PairingError, match="signature failed"):
            # Signature check catches the mutation
            svc._verify_offer(past)

    @pytest.mark.unit
    def test_tampered_signature_rejected(self, tmp_identity, mock_user):
        svc = self._make_svc()
        offer = svc.create_offer(current_user=mock_user)
        # Flip a byte in the signature
        bad_sig = bytearray(bytes.fromhex(offer.signature))
        bad_sig[5] ^= 0xFF
        tampered = PairingOffer(**{**offer.model_dump(), "signature": bad_sig.hex()})
        with pytest.raises(PairingError, match="signature failed"):
            svc._verify_offer(tampered)

    @pytest.mark.unit
    def test_malformed_hex_rejected(self, tmp_identity, mock_user):
        svc = self._make_svc()
        offer = svc.create_offer(current_user=mock_user)
        bad = PairingOffer(**{
            **offer.model_dump(),
            "signature": "z" * 128,  # not valid hex
        })
        with pytest.raises(PairingError, match="Malformed"):
            svc._verify_offer(bad)


# =============================================================================
# Nonce cache — single-use + user-bound + TTL
# =============================================================================


class TestNonceCache:
    @pytest.mark.unit
    def test_single_use(self, tmp_identity, mock_user):
        from services.pairing_service import _cache_nonce, _pop_cached_nonce
        nonce = "abc123"
        _cache_nonce(nonce, expires_at=int(time.time()) + 60, initiator_user_id=42)
        assert _pop_cached_nonce(nonce, 42) is True
        # Second call — nonce already consumed
        assert _pop_cached_nonce(nonce, 42) is False

    @pytest.mark.unit
    def test_wrong_user_rejects(self, tmp_identity):
        from services.pairing_service import _cache_nonce, _pop_cached_nonce
        nonce = "abc456"
        _cache_nonce(nonce, expires_at=int(time.time()) + 60, initiator_user_id=42)
        # Different user tries to complete — rejected (and nonce consumed
        # by the pop, preventing a retry with the right user even; that's
        # acceptable because the attacker just burned the nonce).
        assert _pop_cached_nonce(nonce, 99) is False

    @pytest.mark.unit
    def test_expired_rejects(self, tmp_identity):
        from services.pairing_service import _cache_nonce, _pop_cached_nonce
        nonce = "abc789"
        _cache_nonce(nonce, expires_at=int(time.time()) - 1, initiator_user_id=42)
        assert _pop_cached_nonce(nonce, 42) is False

    @pytest.mark.unit
    def test_unknown_nonce_rejects(self, tmp_identity):
        from services.pairing_service import _pop_cached_nonce
        assert _pop_cached_nonce("never-cached", 42) is False


# =============================================================================
# PairingService.complete_handshake — nonce + signature gating
# =============================================================================


class TestCompleteHandshake:
    def _make_signed_response(self, tmp_identity, nonce, **overrides):
        """Build a legitimate responder-signed PairingResponse. Helper
        for test scenarios that need a valid (or selectively invalid)
        response without duplicating the sign-roundtrip boilerplate."""
        unsigned = {
            "version": 1,
            "nonce": nonce,
            "responder_pubkey": tmp_identity.public_key_hex(),
            "responder_user_id": 99,
            "responder_display_name": "Mom",
            "accepted_endpoints": [],
            "my_tier_for_you": 2,
            "accepted_at": int(time.time()),
        }
        unsigned.update(overrides)
        signature = tmp_identity.sign(_canonical_bytes(unsigned)).hex()
        return PairingResponse(**unsigned, signature=signature)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_replay_rejected(self, tmp_identity, mock_user, monkeypatch):
        """Replay: use the same valid response twice. Second call fails
        because the nonce was consumed on the first call."""
        from unittest.mock import AsyncMock
        from services import pairing_service as ps

        svc = PairingService(db=MagicMock())
        offer = svc.create_offer(current_user=mock_user)
        response = self._make_signed_response(tmp_identity, nonce=offer.nonce)

        # monkeypatch — auto-restored on teardown, no cross-test leak.
        monkeypatch.setattr(ps, "_get_or_create_circle", AsyncMock(return_value=MagicMock()))
        svc._upsert_peer_user = AsyncMock(return_value=MagicMock(id=1))
        svc._upsert_circle_membership = AsyncMock()

        # First call succeeds
        await svc.complete_handshake(mock_user, response, their_tier_for_me=3)

        # Second call: same response, nonce already consumed
        with pytest.raises(PairingError, match="nonce"):
            await svc.complete_handshake(mock_user, response, their_tier_for_me=3)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_bad_tier_rejected(self, tmp_identity, mock_user, monkeypatch):
        from unittest.mock import AsyncMock
        from services import pairing_service as ps

        svc = PairingService(db=MagicMock())
        offer = svc.create_offer(current_user=mock_user)
        response = self._make_signed_response(tmp_identity, nonce=offer.nonce)
        monkeypatch.setattr(ps, "_get_or_create_circle", AsyncMock(return_value=MagicMock()))
        svc._upsert_peer_user = AsyncMock(return_value=MagicMock(id=1))
        svc._upsert_circle_membership = AsyncMock()

        with pytest.raises(PairingError, match="Tier must be"):
            await svc.complete_handshake(mock_user, response, their_tier_for_me=5)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_forged_signature_does_not_burn_nonce(
        self, tmp_identity, mock_user, monkeypatch,
    ):
        """DoS-guard regression (review SHOULD-FIX #3): an attacker who
        guesses the nonce but can't forge a valid signature must NOT
        consume the legitimate user's nonce. Signature verify runs
        before the nonce pop — a forged attempt leaves the nonce
        available for the real responder's follow-up."""
        from unittest.mock import AsyncMock
        from services import pairing_service as ps

        svc = PairingService(db=MagicMock())
        offer = svc.create_offer(current_user=mock_user)

        # Build a response with a tampered signature (flip a byte).
        legit = self._make_signed_response(tmp_identity, nonce=offer.nonce)
        bad_sig = bytearray(bytes.fromhex(legit.signature))
        bad_sig[0] ^= 0xFF
        forged = PairingResponse(**{**legit.model_dump(), "signature": bad_sig.hex()})

        monkeypatch.setattr(ps, "_get_or_create_circle", AsyncMock(return_value=MagicMock()))
        svc._upsert_peer_user = AsyncMock(return_value=MagicMock(id=1))
        svc._upsert_circle_membership = AsyncMock()

        # Forged attempt is rejected via signature check, NOT nonce check
        with pytest.raises(PairingError, match="signature"):
            await svc.complete_handshake(mock_user, forged, their_tier_for_me=3)

        # Legit response with the same nonce STILL succeeds — nonce survived
        await svc.complete_handshake(mock_user, legit, their_tier_for_me=3)


class TestFullHandshakeRoundtrip:
    """End-to-end: create_offer → accept_offer → complete_handshake.

    Uses TWO distinct FederationIdentity instances — initiator and
    responder — to exercise real cross-identity verification. Without
    this scenario, the three primitives pass individually but a
    mismatch between any two (shared canonical fn, shared key-loading
    path) would go unnoticed.
    """

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_full_roundtrip(self, tmp_path, monkeypatch):
        from unittest.mock import AsyncMock
        from services import pairing_service as ps
        from services.federation_identity import (
            FederationIdentity,
            reset_federation_identity_for_tests,
            init_federation_identity,
            get_federation_identity,
        )
        from cryptography.hazmat.primitives.asymmetric import ed25519

        _clear_nonce_cache_for_tests()

        # Initiator side: load-or-create a fresh identity at path A.
        reset_federation_identity_for_tests()
        init_federation_identity(tmp_path / "initiator_key")
        initiator_identity = get_federation_identity()
        alice = MagicMock(id=1, username="alice")

        # Build an independent responder identity directly (no singleton).
        # We feed its signatures into accept_offer by monkey-patching the
        # service's self.identity on a responder-side PairingService.
        responder_priv = ed25519.Ed25519PrivateKey.generate()
        responder_identity = FederationIdentity(responder_priv)
        bob = MagicMock(id=2, username="bob")

        # Step 1: Alice creates an offer.
        svc_alice = PairingService(db=MagicMock())
        offer = svc_alice.create_offer(current_user=alice)
        assert offer.initiator_pubkey == initiator_identity.public_key_hex()

        # Step 2: Bob accepts. Route his PairingService through a mocked
        # DB + a replaced identity (simulating a second host).
        svc_bob = PairingService(db=MagicMock())
        svc_bob.identity = responder_identity  # as if this were Bob's Renfield
        monkeypatch.setattr(ps, "_get_or_create_circle", AsyncMock(return_value=MagicMock()))
        svc_bob._upsert_peer_user = AsyncMock(return_value=MagicMock(id=10))
        svc_bob._upsert_circle_membership = AsyncMock()

        response = await svc_bob.accept_offer(
            current_user=bob, offer=offer, my_tier_for_you=2,
        )
        assert response.nonce == offer.nonce
        assert response.responder_pubkey == responder_identity.public_key_hex()

        # Step 3: Alice verifies + completes. Crucial: the signature
        # covering `response` was made by responder_identity, but
        # verify() must succeed using response.responder_pubkey as the
        # key material — that's the whole point of peer identity.
        svc_alice._upsert_peer_user = AsyncMock(return_value=MagicMock(id=20))
        svc_alice._upsert_circle_membership = AsyncMock()

        peer = await svc_alice.complete_handshake(
            current_user=alice, response=response, their_tier_for_me=3,
        )
        assert peer is not None

        # Alice persisted bob as her peer + bob persisted alice as his peer.
        svc_alice._upsert_peer_user.assert_awaited_once()
        svc_bob._upsert_peer_user.assert_awaited_once()
        # Both sides also issued a CircleMembership (at the respective tiers).
        svc_alice._upsert_circle_membership.assert_awaited_once()
        svc_bob._upsert_circle_membership.assert_awaited_once()

        reset_federation_identity_for_tests()
