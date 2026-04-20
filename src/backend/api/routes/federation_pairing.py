"""
Federation pairing API — the three HTTP endpoints that drive the
QR-code handshake.

Design ref: second-brain-circles v2 § Pairing Handshake State Machine.

Flow:
    POST /api/federation/pair/offer      → initiator: mint signed offer
    POST /api/federation/pair/accept     → responder: verify + persist + respond
    POST /api/federation/pair/complete   → initiator: verify response + persist peer

All three require an authenticated user. The routes are thin wrappers
around `PairingService` — business logic, signatures, and nonce
handling live there.

Error handling: every backend-raised `PairingError` maps to HTTP 400
with a uniform "handshake failed" detail. We deliberately do NOT
surface which check failed (signature / expiry / nonce / tier) —
that's an oracle the adversary-peer threat model calls out.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import User
from services.auth_service import get_current_user
from services.database import get_db
from services.federation_identity import get_federation_identity
from services.pairing_service import (
    PairingError,
    PairingOffer,
    PairingResponse,
    PairingService,
)


router = APIRouter()


# =============================================================================
# Request / response schemas (routes only — wire-format lives in PairingService)
# =============================================================================


class CreateOfferRequest(BaseModel):
    display_name: str | None = None
    offered_endpoints: list[dict] = Field(default_factory=list)


class AcceptOfferRequest(BaseModel):
    offer: PairingOffer
    my_tier_for_you: int = Field(..., ge=0, le=4)
    accepted_endpoints: list[dict] = Field(default_factory=list)


class CompleteHandshakeRequest(BaseModel):
    response: PairingResponse
    their_tier_for_me: int = Field(..., ge=0, le=4)


class IdentityResponse(BaseModel):
    pubkey_hex: str


class PeerUserResponse(BaseModel):
    id: int
    remote_pubkey: str
    remote_display_name: str
    remote_user_id: int | None
    paired_at: str


# =============================================================================
# Routes
# =============================================================================


@router.get("/identity", response_model=IdentityResponse)
async def get_identity(
    _current_user: User = Depends(get_current_user),
):
    """Return this Renfield's Ed25519 public key (for peer display + debugging)."""
    return IdentityResponse(pubkey_hex=get_federation_identity().public_key_hex())


@router.post("/pair/offer", response_model=PairingOffer)
async def create_pair_offer(
    body: CreateOfferRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Initiator step 1 — create a signed offer. Stateless until accepted."""
    svc = PairingService(db)
    return svc.create_offer(
        current_user=current_user,
        display_name=body.display_name,
        offered_endpoints=body.offered_endpoints,
    )


@router.post("/pair/accept", response_model=PairingResponse)
async def accept_pair_offer(
    body: AcceptOfferRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Responder step 2 — verify offer + create PeerUser + sign response."""
    svc = PairingService(db)
    try:
        return await svc.accept_offer(
            current_user=current_user,
            offer=body.offer,
            my_tier_for_you=body.my_tier_for_you,
            accepted_endpoints=body.accepted_endpoints,
        )
    except PairingError as e:
        logger.warning(f"Pairing accept failed for user {current_user.id}: {e}")
        # Uniform error — no oracle on which check failed.
        raise HTTPException(status_code=400, detail="Pairing handshake failed")


@router.post("/pair/complete", response_model=PeerUserResponse)
async def complete_pair_handshake(
    body: CompleteHandshakeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Initiator step 3 — verify response + create PeerUser. Pairing is live afterwards."""
    svc = PairingService(db)
    try:
        peer = await svc.complete_handshake(
            current_user=current_user,
            response=body.response,
            their_tier_for_me=body.their_tier_for_me,
        )
    except PairingError as e:
        logger.warning(f"Pairing complete failed for user {current_user.id}: {e}")
        raise HTTPException(status_code=400, detail="Pairing handshake failed")

    return PeerUserResponse(
        id=peer.id,
        remote_pubkey=peer.remote_pubkey,
        remote_display_name=peer.remote_display_name,
        remote_user_id=peer.remote_user_id,
        paired_at=peer.paired_at.isoformat() if peer.paired_at else "",
    )
