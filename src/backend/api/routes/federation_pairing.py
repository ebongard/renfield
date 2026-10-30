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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import PeerUser, User
from services.auth_service import get_user_or_default
from services.database import get_db
from services.federation_identity import get_federation_identity
from services.pairing_service import (
    AlreadyPairedElsewhere,
    PairingError,
    PairingOffer,
    PairingResponse,
    PairingService,
)
from utils.config import settings

router = APIRouter()


async def _refuse_unresolvable(endpoints: list[dict]) -> None:
    """An override whose host does not resolve is refused.

    Why, found on 2026-10-29: `transport_config` held
    `https://renfield.loca` -- the name was missing its final `l`, AND it was
    the FAR SIDE's name instead of our own. Both came from this field. The
    pairing reported success, and the failure only showed on the first real
    use, days later, as "Name or service not known" deep inside the
    federation client.

    The sanitisation above already discards empty values, on the grounds that
    otherwise "a garbage endpoint" would persist. A typo is garbage too.

    ONLY the input from the UI is checked, not the configured default
    (`federation_advertised_url`). That one is the operator's business, goes
    through review, and may deliberately carry a name this instance does not
    resolve itself (split horizon). The input is what gets mistyped.

    NAME RESOLUTION only, no HTTP request: the latter would be stricter but
    would reject a setup in which the instance cannot reach itself under its
    public name. Resolution alone would have caught the case
    (measured: `renfield.local` -> 192.168.1.230, `renfield.loca` -> error).
    `getaddrinfo` BLOCKS, and both callers are `async`. A failing resolution
    runs into the resolver timeout (seconds) and stalls the entire event loop
    for that long -- hence a thread. `_with_tofu_fingerprint` is async for the
    same reason.
    """
    import asyncio
    import socket
    from urllib.parse import urlparse

    for eintrag in endpoints:
        roh = str(eintrag.get("url", "")).strip()
        host = urlparse(roh).hostname
        if not host:
            raise HTTPException(
                status_code=400,
                detail=f"Die angegebene URL hat keinen Hostnamen: {roh!r}")
        try:
            await asyncio.to_thread(socket.getaddrinfo, host, None)
        except OSError:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Der Name {host!r} ist von dieser Instanz aus nicht "
                    "auflösbar. Gemeint ist die Adresse, unter der die "
                    "GEGENSEITE DIESE Instanz erreicht — nicht die Adresse der "
                    "Gegenseite. Feld leer lassen, um den konfigurierten "
                    "Standard dieser Instanz zu verwenden."),
            ) from None


async def _resolve_advertised_endpoints(provided: list[dict]) -> list[dict]:
    """The endpoints THIS instance advertises during pairing. A per-pairing UI
    override wins; else fall back to the `federation_advertised_url` setting;
    else advertise nothing (legacy — the peer gets no usable endpoint).

    `provided` is scrubbed of entries with a blank/missing `url` before it counts
    as an override — the UI already guards this, but a raw API caller posting
    `[{"url": ""}]` / `[{}]` would otherwise persist a garbage endpoint that
    resolves to "" and silently breaks the peer's transport. An all-blank
    override re-falls-through to the setting."""
    cleaned = [e for e in provided if isinstance(e, dict) and str(e.get("url", "")).strip()]
    if cleaned:
        await _refuse_unresolvable(cleaned)
        return cleaned
    if settings.federation_advertised_url:
        return [{"url": settings.federation_advertised_url}]
    return []


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
    _current_user: User = Depends(get_user_or_default),
):
    """Return this Renfield's Ed25519 public key (for peer display + debugging)."""
    return IdentityResponse(pubkey_hex=get_federation_identity().public_key_hex())


@router.post("/pair/offer", response_model=PairingOffer)
async def create_pair_offer(
    body: CreateOfferRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Initiator step 1 — create a signed offer. Stateless until accepted."""
    svc = PairingService(db)
    return svc.create_offer(
        current_user=current_user,
        display_name=body.display_name,
        offered_endpoints=await _resolve_advertised_endpoints(body.offered_endpoints),
    )


async def _resync_peer_registry(request: Request, db: AsyncSession, reason: str) -> None:
    """Rebuild the `peer_<id>` MCP servers so `query_brain` matches the DB.

    🛑 EVERY route that creates or revokes a pairing must call this. It was
    wired only into `revoke_peer` and app startup, so a pairing made while the
    backend was running stayed INVISIBLE to the agent loop until the next
    restart: the row was in `peer_users`, the tool was not in `_tool_index`,
    and a federated question failed as if the pairing had not happened.
    Measured 2026-10-28 — both pods started 13:35 UTC, both pairings went live
    14:22 UTC, neither tool was registered. The comment in `mcp_client.py`
    claiming a sync "on pair/unpair events" described an intention, not code.

    Non-fatal: the DB stays authoritative and the responder re-checks the peer
    per request, so a failed resync costs discoverability, never safety.
    """
    try:
        manager = getattr(request.app.state, "mcp_manager", None)
        if manager is not None:
            from services.peer_mcp_registry import sync_peers
            await sync_peers(manager, db)
    except Exception as e:
        logger.warning(f"Peer registry resync after {reason} failed (non-fatal): {e}")


@router.post("/pair/accept", response_model=PairingResponse)
async def accept_pair_offer(
    body: AcceptOfferRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Responder step 2 — verify offer + create PeerUser + sign response."""
    svc = PairingService(db)
    try:
        response = await svc.accept_offer(
            current_user=current_user,
            offer=body.offer,
            my_tier_for_you=body.my_tier_for_you,
            accepted_endpoints=await _resolve_advertised_endpoints(body.accepted_endpoints),
        )
    except AlreadyPairedElsewhere as e:
        # 🛑 BEFORE the uniform catch: local state, not a cryptographic
        # finding. Kept opaque it sends the person to the wrong place.
        logger.warning(f"Pairing accept refused for user {current_user.id}: {e}")
        raise HTTPException(status_code=409, detail=str(e)) from e
    except PairingError as e:
        logger.warning(f"Pairing accept failed for user {current_user.id}: {e}")
        # Uniform error — no oracle on which check failed.
        raise HTTPException(status_code=400, detail="Pairing handshake failed")

    await _resync_peer_registry(request, db, "pair/accept")

    return response


@router.post("/pair/complete", response_model=PeerUserResponse)
async def complete_pair_handshake(
    body: CompleteHandshakeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Initiator step 3 — verify response + create PeerUser. Pairing is live afterwards."""
    svc = PairingService(db)
    try:
        peer = await svc.complete_handshake(
            current_user=current_user,
            response=body.response,
            their_tier_for_me=body.their_tier_for_me,
        )
    except AlreadyPairedElsewhere as e:
        logger.warning(f"Pairing complete refused for user {current_user.id}: {e}")
        raise HTTPException(status_code=409, detail=str(e)) from e
    except PairingError as e:
        logger.warning(f"Pairing complete failed for user {current_user.id}: {e}")
        raise HTTPException(status_code=400, detail="Pairing handshake failed")

    await _resync_peer_registry(request, db, "pair/complete")

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


def _tier_for_peer(peer: PeerUser) -> int:
    """The tier this owner granted the peer at pairing time.

    Since F-ID-3 it lives ON THE PAIRING. Until 2026-10-27 this looked up a
    `CircleMembership` whose `member_user_id` was the REMOTE user id -- with
    `scalar_one_or_none()`, which with two peers under one owner (both number
    their users from 1) would have raised `MultipleResultsFound` and, in the
    SQL branch, matched the WRONG peer's grant (BL-0135). This column removes
    both structurally: one pairing, one row, one tier.

    The column is `NOT NULL` with a default of 4 -- there is no failure case
    left that would need a fail-closed answer.
    """
    return peer.granted_tier


@router.get("/peers", response_model=PeerListResponse)
async def list_peers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
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
        tier = _tier_for_peer(peer)
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
    current_user: User = Depends(get_user_or_default),
):
    """Revoke a paired peer. Side effects:
      - PeerUser.revoked_at := now (the row stays for audit trail)
      - PeerUser.granted_tier := 4, the narrowest rung, so a later
        re-pairing cannot silently inherit the old grant
      - MCPManager peer registry re-synced so `mcp.peer_<id>.query_brain`
        vanishes from the agent loop's tool surface

    Owner-scoped: only the account that owns the pairing can revoke it,
    and a peer owned by someone else answers 404 like a missing one.
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

    # Until 2026-10-27 there was a DELETE on `circle_memberships` here with
    # `member_user_id = peer.remote_user_id`. That was the bug itself -- the
    # row never belonged there -- and on top of that it deleted WITHOUT a
    # `dimension` filter, so it also took `tenant` and `project` rows of the
    # same pair. The peer's reach now depends on `revoked_at` and
    # `granted_tier`; both live on its own row.
    #
    # `granted_tier` is reset along with it, so a later un-revoke does not
    # silently inherit the old grant.
    peer.granted_tier = 4

    await db.commit()

    # The `CircleResolver` cache is no longer cleared: it holds
    # `(circle_owner_id, member_user_id)` pairs, and a peer no longer has such
    # a pair. In-flight requests are cleaned up by the call below.

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

    # Same helper as the two pairing routes — `mcp.peer_<id>.query_brain`
    # disappears from the agent loop here instead of appearing.
    await _resync_peer_registry(request, db, "revoke")

    logger.info(
        f"🔗 Peer {peer.remote_display_name} (id={peer.id}) revoked by "
        f"user {current_user.id}"
    )
