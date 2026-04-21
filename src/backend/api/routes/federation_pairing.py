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

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import CircleMembership, PeerUser, User
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


class PeerDetailResponse(BaseModel):
    """Richer shape for the /settings/circles/peers page — includes tier
    granted + last-seen timestamp so the UI can render relative-time
    labels and the tier-badge."""
    id: int
    remote_pubkey: str
    remote_display_name: str
    remote_user_id: int | None
    paired_at: str
    last_seen_at: str | None
    circle_tier: int  # the tier THIS user granted the remote peer (their view into us)


class PeerListResponse(BaseModel):
    peers: list[PeerDetailResponse]


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


# =============================================================================
# Peer management (F4a)
# =============================================================================


async def _tier_for_peer(
    db: AsyncSession, owner_id: int, remote_user_id: int | None,
) -> int:
    """Look up the tier this owner granted the given peer at pair time.

    Fail-closed fallback: returns tier 0 (self / most private) on any
    data-integrity failure (missing membership row, non-integer value,
    missing remote_user_id). Tier 4 would mean PUBLIC, which is the
    opposite of defensive — a row displayed with a permissive tier
    badge on the peers page could mislead the owner into thinking
    they'd shared broadly when the underlying record is missing. The
    F2 pairing flow always writes a membership, so this fallback only
    fires on data-integrity bugs, and 0 is the safer wrong answer.
    """
    if remote_user_id is None:
        return 0
    row = (await db.execute(
        select(CircleMembership).where(
            CircleMembership.circle_owner_id == owner_id,
            CircleMembership.member_user_id == remote_user_id,
            CircleMembership.dimension == "tier",
        )
    )).scalar_one_or_none()
    if row is None:
        return 0
    try:
        return int(row.value)
    except (TypeError, ValueError):
        return 0


@router.get("/peers", response_model=PeerListResponse)
async def list_peers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List the authenticated user's paired peers (non-revoked only).

    Response carries the tier the local user granted each peer so the
    UI can render tier-badges + make the "re-tier a peer" surface
    simple. Last-seen timestamp lets the UI show "last seen 2 hours ago".
    """
    rows = (await db.execute(
        select(PeerUser).where(
            PeerUser.circle_owner_id == current_user.id,
            PeerUser.revoked_at.is_(None),
        ).order_by(PeerUser.paired_at.desc())
    )).scalars().all()

    peers = []
    for peer in rows:
        tier = await _tier_for_peer(db, current_user.id, peer.remote_user_id)
        peers.append(PeerDetailResponse(
            id=peer.id,
            remote_pubkey=peer.remote_pubkey,
            remote_display_name=peer.remote_display_name,
            remote_user_id=peer.remote_user_id,
            paired_at=peer.paired_at.isoformat() if peer.paired_at else "",
            last_seen_at=peer.last_seen_at.isoformat() if peer.last_seen_at else None,
            circle_tier=tier,
        ))
    return PeerListResponse(peers=peers)


@router.delete("/peers/{peer_id}", status_code=204)
async def revoke_peer(
    peer_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Revoke a paired peer. Side effects:
      - PeerUser.revoked_at := now (the row stays for audit trail)
      - CircleMembership for this peer deleted (they no longer reach
        any atoms at their old tier; F3 read-time verify already
        short-circuits on revoked_at, but the membership cleanup is
        the authoritative revoke)
      - MCPManager peer registry re-synced so `mcp.peer_<id>.query_brain`
        vanishes from the agent loop's tool surface
    """
    peer = (await db.execute(
        select(PeerUser).where(
            PeerUser.id == peer_id,
            PeerUser.circle_owner_id == current_user.id,
        )
    )).scalar_one_or_none()
    if peer is None:
        # Uniform 404 whether the peer doesn't exist OR belongs to
        # another user — no existence oracle on peer ids.
        raise HTTPException(status_code=404, detail="Peer not found")

    peer.revoked_at = datetime.now(UTC).replace(tzinfo=None)

    # Delete the tier membership so their circle reach drops to zero
    # immediately. F3 retrieval paths also check revoked_at on the
    # PeerUser row, but circle_memberships is the authoritative record.
    if peer.remote_user_id is not None:
        await db.execute(
            delete(CircleMembership).where(
                CircleMembership.circle_owner_id == current_user.id,
                CircleMembership.member_user_id == peer.remote_user_id,
            )
        )

    await db.commit()

    # Invalidate the CircleResolver class-level cache so any in-flight
    # handler that already cached (owner, member) → tier drops the stale
    # entry. Without this, retrieval paths keep resolving the peer at
    # their pre-revocation reach until process restart.
    if peer.remote_user_id is not None:
        from services.circle_resolver import CircleResolver
        CircleResolver.invalidate_for_membership(
            current_user.id, peer.remote_user_id,
        )

    # Purge in-flight pending query_brain requests bound to this peer's
    # pubkey. Without this, a revoked peer could still poll /retrieve
    # with a request_id from before the revocation and get an answer —
    # handle_retrieve doesn't re-check revoked_at (the check is in
    # handle_initiate only). Purging here closes that window in O(pending).
    from services.federation_query_responder import purge_requests_for_pubkey
    discarded = await purge_requests_for_pubkey(peer.remote_pubkey)
    if discarded:
        logger.info(
            f"🔗 Purged {discarded} in-flight query_brain request(s) "
            f"for revoked peer {peer.remote_display_name}"
        )

    # Refresh the MCP registry so `mcp.peer_<id>.query_brain` disappears
    # from the agent loop. Non-fatal on failure — the DB is authoritative
    # and F3's per-request peer lookup will reject the tool anyway.
    try:
        manager = getattr(request.app.state, "mcp_manager", None)
        if manager is not None:
            from services.peer_mcp_registry import sync_peers
            await sync_peers(manager, db)
    except Exception as e:
        logger.warning(f"Peer registry resync after revoke failed (non-fatal): {e}")

    logger.info(
        f"🔗 Peer {peer.remote_display_name} (id={peer.id}) revoked by "
        f"user {current_user.id}"
    )
