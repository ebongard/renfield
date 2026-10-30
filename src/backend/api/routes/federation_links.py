"""F-ID-2 -- the routes of the person handshake.

The SELF-SERVICE path to a person link, replacing the admin assertion in
`federation_user_links.py`. The difference is not convenience but the kind of
evidence: there an administrator types in two account numbers, here the person
proves control over BOTH accounts by running the handshake signed in on both
instances.

That is why there is NO `require_permission(Permission.ADMIN)` here. That is
the point of the whole thing; putting it back turns this into the admin
assertion with more steps.

Service: `services/link_handshake_service.py` (the limits of the procedure and
why the nonce is NOT a wall against replay are documented there).
"""
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import FederationUserLink, PeerUser, User
from services.auth_service import get_user_or_default
from services.database import get_db
from services.federation_identity_link import delete_link
from services.link_handshake_service import (
    LinkConfirmation,
    LinkHandshakeError,
    LinkHandshakeService,
    LinkOffer,
)

router = APIRouter()


class CreateLinkOfferRequest(BaseModel):
    #: The peer row to link with -- a completed instance pairing is a
    #: precondition, not part of the handshake.
    peer_id: int


class AcceptLinkOfferRequest(BaseModel):
    offer: LinkOffer


class CompleteLinkRequest(BaseModel):
    #: The offer comes back because the server remembered NOTHING. It carries
    #: our own signature, which is verified at completion --
    #: kryptografische Bindung statt Sitzungszustand im Arbeitsspeicher.
    offer: LinkOffer
    confirmation: LinkConfirmation


class LinkResponse(BaseModel):
    id: int
    peer_id: int
    peer_display_name: str
    created_at: str


def _reject(e: LinkHandshakeError, user_id: int) -> HTTPException:
    """One single, always identical 400.

    🛑 No oracle: a different message per cause would give away WHICH check
    fired -- expired, wrong recipient, bad signature. The reason is in the log,
    not in the response. Same pattern as `federation_pairing`
    ("Pairing handshake failed").
    """
    logger.warning(f"Link handshake failed for user {user_id}: {e}")
    return HTTPException(status_code=400, detail="Link handshake failed")


@router.post("/links/offer", response_model=LinkOffer)
async def create_link_offer(
    body: CreateLinkOfferRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Step 1 (on A, as U) -- signed offer. Writes NOTHING."""
    peer = (await db.execute(
        select(PeerUser).where(
            PeerUser.id == body.peer_id,
            PeerUser.circle_owner_id == current_user.id,
            PeerUser.revoked_at.is_(None),
        )
    )).scalar_one_or_none()
    if peer is None:
        raise HTTPException(status_code=404, detail="Unknown or revoked peer")
    return LinkHandshakeService(db).create_offer(current_user, peer)


@router.post("/links/accept", response_model=LinkConfirmation)
async def accept_link_offer(
    body: AcceptLinkOfferRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Step 2 (on B, as X) -- consent, write our own row, sign."""
    try:
        confirmation = await LinkHandshakeService(db).accept_offer(
            current_user, body.offer,
        )
        await db.commit()
    except LinkHandshakeError as e:
        await db.rollback()
        raise _reject(e, current_user.id) from e
    except IntegrityError:
        # The uniqueness index on (peer_id, querier_ref) IS the real guard
        # against a replay -- see the service's module docstring. This is where
        # it fires.
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="This link already exists",
        ) from None
    return confirmation


@router.post("/links/complete", response_model=LinkResponse)
async def complete_link(
    body: CompleteLinkRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Step 3 (back on A, as U) -- verify, write our own row."""
    try:
        link = await LinkHandshakeService(db).complete(
            current_user, body.offer, body.confirmation,
        )
        await db.commit()
    except LinkHandshakeError as e:
        await db.rollback()
        raise _reject(e, current_user.id) from e
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="This link already exists",
        ) from None
    return await _to_response(db, link)


@router.get("/links/mine", response_model=list[LinkResponse])
async def list_my_links(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """This person's OWN links.

    Filtered hard on `local_user_id == current_user.id`, not on the peer
    owner. And without `querier_ref` in the response: that is the opaque band
    between two instances, not a display value -- whoever holds it can present
    themselves to the far side as this person.
    """
    rows = (await db.execute(
        select(FederationUserLink, PeerUser)
        .join(PeerUser, PeerUser.id == FederationUserLink.peer_id)
        .where(FederationUserLink.local_user_id == current_user.id)
        .order_by(FederationUserLink.created_at.desc())
    )).all()
    return [
        LinkResponse(
            id=link.id,
            peer_id=link.peer_id,
            peer_display_name=peer.remote_display_name,
            created_at=link.created_at.isoformat() if link.created_at else "",
        )
        for link, peer in rows
    ]


@router.delete("/links/{link_id}", status_code=204)
async def revoke_my_link(
    link_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Revoke your own link -- unilateral and effective immediately."""
    row = (await db.execute(
        select(FederationUserLink, PeerUser)
        .join(PeerUser, PeerUser.id == FederationUserLink.peer_id)
        .where(
            FederationUserLink.id == link_id,
            # 404 rather than 403 for someone else's id: a 403 would confirm
            # that this link EXISTS.
            FederationUserLink.local_user_id == current_user.id,
        )
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown link")
    link, peer = row

    await delete_link(db, link_id=link.id)
    await db.commit()

    # 🛑 Clean up requests in flight, the way `revoke_peer` does:
    # `handle_retrieve` does NOT re-check the link, so a request started before
    # the revocation could still be collected with full reach.
    # Deliberately too coarse -- it also hits unlinked requests from the same
    # peer -- and in exchange it cannot miss the mapped one.
    from services.federation_query_responder import purge_requests_for_pubkey
    discarded = await purge_requests_for_pubkey(peer.remote_pubkey)
    logger.info(
        f"🔗 Verknuepfung {link_id} widerrufen; {discarded} laufende Anfrage(n) verworfen"
    )


async def _to_response(db: AsyncSession, link: FederationUserLink) -> LinkResponse:
    peer = (await db.execute(
        select(PeerUser).where(PeerUser.id == link.peer_id)
    )).scalar_one()
    return LinkResponse(
        id=link.id,
        peer_id=link.peer_id,
        peer_display_name=peer.remote_display_name,
        created_at=link.created_at.isoformat() if link.created_at else "",
    )
