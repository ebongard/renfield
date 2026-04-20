"""
PairingService — backend for the QR-code federation handshake.

Three-step protocol (design ref: second-brain-circles v2 § Pairing
Handshake State Machine):

  1. Initiator calls create_offer() on their own Renfield. Gets a signed
     PairingOffer (serializable to QR). Also gets a short-lived nonce
     cached server-side; only that exact nonce can be completed later.

  2. Responder's Renfield receives the scanned offer (via frontend) and
     calls accept_offer(). Verifies the initiator's signature, checks
     the offer hasn't expired, persists a PeerUser row on the responder
     side, creates a CircleMembership at `my_tier_for_you`, and returns
     a signed PairingResponse for the initiator to ingest.

  3. Initiator calls complete_handshake() with the scanned response.
     Verifies the responder's signature, validates the cached nonce,
     persists a PeerUser row on the initiator side, creates a matching
     CircleMembership. The pairing is live.

Adversary model (design doc § Threat Model):
  - Offers expire in 10 minutes. The nonce cache is TTL'd + single-use.
  - Signatures use the Ed25519 identity per host (federation_identity.py).
  - At every step: signature verification is mandatory; expiry checked
    against the responder's clock; nonce validated before any state
    mutation. Failed verification returns a uniform error — no oracle
    (e.g. "your signature failed" vs "nonce stale" telegraphs info).

This module is the backend. F4 (frontend) adds the QR encode/scan UX.
F3 (query_brain) consumes PeerUser rows created here.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    Circle,
    CircleMembership,
    PeerUser,
    User,
)
from services.federation_identity import get_federation_identity


OFFER_TTL_SECONDS = 600  # 10 minutes — enough to scan + accept, short
                         # enough that a leaked QR code doesn't outlive its usefulness
NONCE_CACHE_MAX = 1024   # bounded LRU; production sees ≤1 pairing per minute


# =============================================================================
# Wire-format schemas (Pydantic) — what goes into QR codes and over HTTP
# =============================================================================


class PairingOffer(BaseModel):
    """
    Initiator-signed pairing invitation. Encoded into the QR-code scanned
    by the responder's phone/tablet. Round-tripped verbatim — the signature
    covers every non-signature field as JSON with sorted keys.
    """
    version: int = 1
    initiator_pubkey: str = Field(..., min_length=64, max_length=64)  # hex
    initiator_user_id: int
    display_name: str = Field(..., max_length=255)
    offered_endpoints: list[dict[str, Any]] = Field(default_factory=list)
    nonce: str = Field(..., min_length=16)
    issued_at: int  # unix seconds
    expires_at: int  # unix seconds
    signature: str = Field(..., min_length=128, max_length=128)  # hex


class PairingResponse(BaseModel):
    """
    Responder-signed completion of a handshake. Returned to the initiator
    (typically by the responder's frontend displaying a second QR, or via
    Tailscale round-trip in fully-automated flows).
    """
    version: int = 1
    nonce: str = Field(..., min_length=16)  # echoes initiator's nonce
    responder_pubkey: str = Field(..., min_length=64, max_length=64)
    responder_user_id: int
    responder_display_name: str = Field(..., max_length=255)
    accepted_endpoints: list[dict[str, Any]] = Field(default_factory=list)
    # Tier the RESPONDER grants the INITIATOR (responder's view of initiator).
    my_tier_for_you: int = Field(..., ge=0, le=4)
    accepted_at: int  # unix seconds
    signature: str = Field(..., min_length=128, max_length=128)


# =============================================================================
# Exceptions (all wrapped in a uniform HTTPException at the route layer)
# =============================================================================


class PairingError(Exception):
    """Any handshake failure — signature/expiry/nonce/FK/tier. Uniform
    at the API boundary so we don't leak which check failed."""


# =============================================================================
# Nonce cache (single-process LRU)
# =============================================================================


@dataclass
class _CachedNonce:
    expires_at: int
    initiator_user_id: int


# Module-level cache. Single-process since Renfield ships one backend
# container; a multi-process deploy would need Redis (noted in F5 hardening).
_nonce_cache: dict[str, _CachedNonce] = {}


def _cache_nonce(nonce: str, expires_at: int, initiator_user_id: int) -> None:
    # Evict the oldest if over capacity — bounded so a leak can't DoS memory.
    if len(_nonce_cache) >= NONCE_CACHE_MAX:
        oldest = min(_nonce_cache.items(), key=lambda kv: kv[1].expires_at)
        _nonce_cache.pop(oldest[0], None)
    _nonce_cache[nonce] = _CachedNonce(expires_at=expires_at, initiator_user_id=initiator_user_id)


def _pop_cached_nonce(nonce: str, initiator_user_id: int) -> bool:
    """
    Single-use + bound to the issuing user. Returns True iff the nonce
    was present, not expired, and issued to this user.
    """
    cached = _nonce_cache.pop(nonce, None)
    if cached is None:
        return False
    if cached.initiator_user_id != initiator_user_id:
        return False
    if cached.expires_at < int(time.time()):
        return False
    return True


def _clear_nonce_cache_for_tests() -> None:
    """Test-only — resets the cache between tests."""
    _nonce_cache.clear()


# =============================================================================
# Service
# =============================================================================


class PairingService:
    """
    One per AsyncSession (request scope). The federation identity is
    process-wide (loaded once in federation_identity).
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.identity = get_federation_identity()

    # -------------------------------------------------------------------------
    # Step 1 — initiator creates offer
    # -------------------------------------------------------------------------

    def create_offer(
        self,
        current_user: User,
        display_name: str | None = None,
        offered_endpoints: list[dict[str, Any]] | None = None,
    ) -> PairingOffer:
        """
        Mint a signed offer + cache the nonce so only the matching
        response completes the handshake. No DB write — the offer is
        stateless until the responder accepts.
        """
        now = int(time.time())
        nonce = secrets.token_hex(16)  # 32-char hex, 128 bits of entropy
        expires_at = now + OFFER_TTL_SECONDS

        unsigned = {
            "version": 1,
            "initiator_pubkey": self.identity.public_key_hex(),
            "initiator_user_id": current_user.id,
            "display_name": display_name or current_user.username or f"user#{current_user.id}",
            "offered_endpoints": offered_endpoints or [],
            "nonce": nonce,
            "issued_at": now,
            "expires_at": expires_at,
        }
        signature = self.identity.sign(_canonical_bytes(unsigned)).hex()

        _cache_nonce(nonce, expires_at=expires_at, initiator_user_id=current_user.id)

        return PairingOffer(**unsigned, signature=signature)

    # -------------------------------------------------------------------------
    # Step 2 — responder accepts
    # -------------------------------------------------------------------------

    async def accept_offer(
        self,
        current_user: User,
        offer: PairingOffer,
        my_tier_for_you: int,
        accepted_endpoints: list[dict[str, Any]] | None = None,
    ) -> PairingResponse:
        """
        Verify the offer + create the responder-side PeerUser row +
        issue a CircleMembership placing the initiator at `my_tier_for_you`.
        Returns a signed response for the initiator to ingest.
        """
        self._verify_offer(offer)

        if not (0 <= my_tier_for_you <= 4):
            raise PairingError("Tier must be between 0 and 4")

        # Persist the initiator as our peer + as a member of our circle at the chosen tier.
        await self._upsert_peer_user(
            owner_user_id=current_user.id,
            remote_pubkey=offer.initiator_pubkey,
            remote_display_name=offer.display_name,
            remote_user_id=offer.initiator_user_id,
            transport_config={"endpoints": offer.offered_endpoints},
        )
        # Ensure the responder has a circles row (needed for membership FK).
        await _get_or_create_circle(self.db, current_user.id)
        await self._upsert_circle_membership(
            circle_owner_id=current_user.id,
            member_user_id=offer.initiator_user_id,
            tier=my_tier_for_you,
            granted_by=current_user.id,
        )

        now = int(time.time())
        unsigned = {
            "version": 1,
            "nonce": offer.nonce,
            "responder_pubkey": self.identity.public_key_hex(),
            "responder_user_id": current_user.id,
            "responder_display_name": current_user.username or f"user#{current_user.id}",
            "accepted_endpoints": accepted_endpoints or [],
            "my_tier_for_you": my_tier_for_you,
            "accepted_at": now,
        }
        signature = self.identity.sign(_canonical_bytes(unsigned)).hex()
        return PairingResponse(**unsigned, signature=signature)

    # -------------------------------------------------------------------------
    # Step 3 — initiator completes
    # -------------------------------------------------------------------------

    async def complete_handshake(
        self,
        current_user: User,
        response: PairingResponse,
        their_tier_for_me: int,
    ) -> PeerUser:
        """
        Verify the responder's signature + the cached nonce + create the
        initiator-side PeerUser row. `their_tier_for_me` is the tier the
        initiator's local user wants to grant the responder (completes
        the bidirectional trust — two independent tiers, one per direction).

        Check order matters: signature FIRST (non-mutating), then nonce
        pop (single-use mutation). A forged signature submitted with a
        guessed nonce would otherwise burn the legitimate user's nonce
        and DoS their in-flight pairing. Signature verify is cheap and
        catches 100% of forged responses before we touch state.
        """
        # Signature verification first — non-mutating, catches forgeries.
        self._verify_response(response)

        # Only now consume the nonce (single-use, user-bound).
        if not _pop_cached_nonce(response.nonce, current_user.id):
            raise PairingError("Handshake nonce expired, unknown, or issued to a different user")

        if not (0 <= their_tier_for_me <= 4):
            raise PairingError("Tier must be between 0 and 4")

        peer = await self._upsert_peer_user(
            owner_user_id=current_user.id,
            remote_pubkey=response.responder_pubkey,
            remote_display_name=response.responder_display_name,
            remote_user_id=response.responder_user_id,
            transport_config={"endpoints": response.accepted_endpoints},
        )
        await _get_or_create_circle(self.db, current_user.id)
        await self._upsert_circle_membership(
            circle_owner_id=current_user.id,
            member_user_id=response.responder_user_id,
            tier=their_tier_for_me,
            granted_by=current_user.id,
        )
        return peer

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    def _verify_offer(self, offer: PairingOffer) -> None:
        if offer.version != 1:
            raise PairingError("Unsupported pairing offer version")
        now = int(time.time())
        if offer.issued_at > now + 60:
            raise PairingError("Offer issued in the future — clock skew")
        if offer.expires_at < now:
            raise PairingError("Offer expired")

        unsigned = offer.model_dump(exclude={"signature"})
        try:
            pubkey = bytes.fromhex(offer.initiator_pubkey)
            signature = bytes.fromhex(offer.signature)
        except ValueError as e:
            raise PairingError("Malformed pubkey or signature hex") from e
        if not self.identity.verify(pubkey, signature, _canonical_bytes(unsigned)):
            raise PairingError("Offer signature failed verification")

    def _verify_response(self, response: PairingResponse) -> None:
        if response.version != 1:
            raise PairingError("Unsupported pairing response version")
        unsigned = response.model_dump(exclude={"signature"})
        try:
            pubkey = bytes.fromhex(response.responder_pubkey)
            signature = bytes.fromhex(response.signature)
        except ValueError as e:
            raise PairingError("Malformed pubkey or signature hex") from e
        if not self.identity.verify(pubkey, signature, _canonical_bytes(unsigned)):
            raise PairingError("Response signature failed verification")

    async def _upsert_peer_user(
        self,
        *,
        owner_user_id: int,
        remote_pubkey: str,
        remote_display_name: str,
        remote_user_id: int | None,
        transport_config: dict[str, Any],
    ) -> PeerUser:
        existing = (await self.db.execute(
            select(PeerUser).where(
                PeerUser.circle_owner_id == owner_user_id,
                PeerUser.remote_pubkey == remote_pubkey,
            )
        )).scalar_one_or_none()

        now = datetime.now(UTC).replace(tzinfo=None)
        if existing is not None:
            existing.remote_display_name = remote_display_name
            existing.remote_user_id = remote_user_id
            existing.transport_config = transport_config
            existing.last_seen_at = now
            existing.revoked_at = None
            await self.db.commit()
            await self.db.refresh(existing)
            return existing

        peer = PeerUser(
            circle_owner_id=owner_user_id,
            remote_pubkey=remote_pubkey,
            remote_display_name=remote_display_name,
            remote_user_id=remote_user_id,
            transport_config=transport_config,
            paired_at=now,
            last_seen_at=now,
        )
        self.db.add(peer)
        try:
            await self.db.commit()
        except IntegrityError:
            # Race: two accept calls hit simultaneously. Roll back, re-SELECT,
            # return the now-existing row.
            await self.db.rollback()
            existing = (await self.db.execute(
                select(PeerUser).where(
                    PeerUser.circle_owner_id == owner_user_id,
                    PeerUser.remote_pubkey == remote_pubkey,
                )
            )).scalar_one_or_none()
            if existing is None:
                raise
            return existing
        await self.db.refresh(peer)
        return peer

    async def _upsert_circle_membership(
        self,
        *,
        circle_owner_id: int,
        member_user_id: int,
        tier: int,
        granted_by: int,
    ) -> CircleMembership:
        existing = (await self.db.execute(
            select(CircleMembership).where(
                CircleMembership.circle_owner_id == circle_owner_id,
                CircleMembership.member_user_id == member_user_id,
                CircleMembership.dimension == "tier",
            )
        )).scalar_one_or_none()

        if existing is not None:
            existing.value = tier
            await self.db.commit()
            await self.db.refresh(existing)
            return existing

        membership = CircleMembership(
            circle_owner_id=circle_owner_id,
            member_user_id=member_user_id,
            dimension="tier",
            value=tier,
            granted_by=granted_by,
        )
        self.db.add(membership)
        await self.db.commit()
        await self.db.refresh(membership)
        return membership


# =============================================================================
# Helpers
# =============================================================================


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """
    Canonical JSON (sorted keys, compact separators, ASCII-escaped,
    NaN/Infinity rejected) is the byte sequence the Ed25519 signature
    covers on both ends. Any mismatch between signer and verifier
    canonicalisation means every signature fails — keep this one
    implementation across create_offer, accept_offer, complete_handshake.

    Explicit flags defend against:
      - `allow_nan=False`: Python's json.dumps emits non-standard
        `NaN`/`Infinity` literals by default; most non-Python JSON
        libraries reject them. Even though F2 only runs Python peers,
        the signature must survive any later peer written in Go/Rust/
        Node (Reva enterprise deployments — per project memory).
        A malicious initiator embedding `float("nan")` into
        `offered_endpoints` would produce a signature that non-Python
        peers can never re-verify.
      - `ensure_ascii=True`: matches the default, made explicit so
        peers that override it (to support non-ASCII display names
        natively) don't silently drift.

    Consumers must ensure `offered_endpoints`/`accepted_endpoints` carry
    only JSON-safe primitives (str/int/bool/None/list/dict). Pydantic's
    dict passthrough doesn't reject floats — callers own that validation.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


async def _get_or_create_circle(db: AsyncSession, owner_user_id: int) -> Circle:
    """
    Ensure a `circles` row exists for the pairing user (needed for the
    circle_memberships FK). Mirrors the helper in api/routes/circles.py:
    IntegrityError recovery for concurrent-first-hit races.
    """
    existing = (await db.execute(
        select(Circle).where(Circle.owner_user_id == owner_user_id)
    )).scalar_one_or_none()
    if existing is not None:
        return existing

    new_circle = Circle(
        owner_user_id=owner_user_id,
        dimension_config={
            "tier": {
                "shape": "ladder",
                "values": ["self", "trusted", "household", "extended", "public"],
            },
        },
        default_capture_policy={"tier": 0},
    )
    db.add(new_circle)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = (await db.execute(
            select(Circle).where(Circle.owner_user_id == owner_user_id)
        )).scalar_one_or_none()
        if existing is None:
            raise
        return existing
    await db.refresh(new_circle)
    return new_circle
