"""
Federation identity-link admin API (F-ID-1 — person-scoped federation).

🛑 ESCAPE HATCH, not the regular path. Since 2026-10-27 a person establishes the
link THEMSELVES, through the consent-signed handshake in
`api/routes/federation_links.py` -- signed in on both instances, each side
signing its own half. This module remains for the case where the handshake is
not possible (one UI is unreachable, a row has to be straightened out by hand).
Using it as the normal path trades a proof for an assertion.

ADMIN-gated access to `federation_user_links` — the cross-instance person map
that lets a federated query be served AS a mapped local user (full circle
reach) instead of the peer-scoped public/guest fallback.

🛑 What `admin` buys here, and what it does not (2026-11-14):

- CREATE: only for the SIGNED-IN account. The linked account is taken from the
  session, never from the body. Until this date the body named it, so an admin
  could map a `querier_ref` to ANY local account on ANY active pairing -- and
  the far side, presenting that ref, was served with that account's full own
  reach (tier 0 included) without that account ever consenting. `admin` is not
  a licence to read another account's data, and it is not one to lend it out.
  An admin may link ITSELF on a pairing it does not own: every account may use
  every active pairing, and the reach lent is the admin's own.
- LIST: every link, but the `querier_ref` only of the caller's own. The ref is
  what the far side maps to ITS account; an admin who could read another
  account's ref could delete that link, re-create it under the own account and
  then ask the far side as that other person.
- DELETE: any link. Taking reach away is safe to delegate; the act is recorded
  in `federation_query_log` (`services.federation_audit.record_revocation`).

The `querier_ref` is the shared opaque token both sides of a pairing must hold:
- On the RESPONDER instance, the link means "ref X → my local user Y".
- On the ASKER instance, the link means "my local user Y, querying this peer,
  presents ref X".
So to make a person's mapping work end-to-end, the SAME (peer, ref) pair is
created on both instances, each by the account it links there. Create on one
side without a ref to mint one, then reuse that ref on the other.

Design: docs/design/federation-identity-mapping.md.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import FederationUserLink, PeerUser, User
from models.permissions import Permission
from services.auth_service import get_user_or_default, require_permission
from services.database import get_db
from services.federation_audit import (
    BASIS_ADMIN,
    BASIS_SELF,
    LINK_REVOKED,
    record_revocation,
)
from services.federation_identity_link import (
    create_link,
    delete_link,
    list_links,
)

router = APIRouter()


class CreateLinkRequest(BaseModel):
    peer_id: int
    # 🛑 NOT who gets linked -- that is the signed-in account. The field is
    # still accepted so an old caller gets a clear 403 instead of silently
    # linking a different account than the one it named.
    local_user_id: int | None = None
    # Omit to MINT a fresh ref (first side of a pairing); pass the minted ref
    # when creating the mirror link on the other instance. Bounded to the DB
    # column width (String(128)) so an oversized ref is a clean 422, not a 500
    # DataError the IntegrityError handler wouldn't catch.
    querier_ref: str | None = Field(default=None, max_length=128)


class LinkResponse(BaseModel):
    id: int
    peer_id: int
    #: `None` on every link that is not the caller's own.
    querier_ref: str | None
    local_user_id: int | None
    created_by: int | None


def _to_response(link: FederationUserLink, *, viewer_id: int) -> LinkResponse:
    own = link.local_user_id == viewer_id
    return LinkResponse(
        id=link.id, peer_id=link.peer_id,
        querier_ref=link.querier_ref if own else None,
        local_user_id=link.local_user_id, created_by=link.created_by,
    )


@router.get("/user-links", response_model=list[LinkResponse])
async def list_user_links(
    request: Request,
    peer_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
    actor: User = Depends(get_user_or_default),
) -> list[LinkResponse]:
    """List identity links (optionally filtered by peer). The `querier_ref` is
    returned for the caller's OWN links only, to mirror on the other instance."""
    links = await list_links(db, peer_id=peer_id)
    return [_to_response(link, viewer_id=actor.id) for link in links]


@router.post("/user-links", response_model=LinkResponse)
async def create_user_link(
    request: Request,
    body: CreateLinkRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
    actor: User = Depends(get_user_or_default),
) -> LinkResponse:
    """Assert a person link for the SIGNED-IN account. 403 if the body names a
    different account, 404 for an unknown or revoked peer, 409 if a link already
    exists for (peer, querier_ref). Links may be created while the feature is
    dark (`federation_identity_links_enabled=False`); they take effect only
    once it's enabled."""
    if body.local_user_id is not None and body.local_user_id != actor.id:
        logger.warning(
            f"Federation link assertion refused: user {actor.id} named "
            f"account {body.local_user_id}"
        )
        raise HTTPException(
            status_code=403,
            detail="A link can only be created for the signed-in account",
        )

    peer = (await db.execute(
        select(PeerUser).where(PeerUser.id == body.peer_id, PeerUser.revoked_at.is_(None))
    )).scalar_one_or_none()
    if peer is None:
        raise HTTPException(status_code=404, detail="Unknown or revoked peer")

    try:
        link = await create_link(
            db, peer_id=peer.id, local_user_id=actor.id,
            querier_ref=body.querier_ref, created_by=actor.id,
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Link already exists for this (peer, querier_ref)",
        ) from None
    return _to_response(link, viewer_id=actor.id)


@router.delete("/user-links/{link_id}")
async def delete_user_link(
    request: Request,
    link_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
    actor: User = Depends(get_user_or_default),
) -> dict:
    """Revoke a person link → that person reverts to the peer-scoped fallback."""
    row = (await db.execute(
        select(FederationUserLink, PeerUser)
        .join(PeerUser, PeerUser.id == FederationUserLink.peer_id)
        .where(FederationUserLink.id == link_id)
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Link not found")
    link, peer = row
    own = link.local_user_id == actor.id

    await delete_link(db, link_id=link.id)
    record_revocation(
        db,
        action=LINK_REVOKED,
        basis=BASIS_SELF if own else BASIS_ADMIN,
        actor_user_id=actor.id,
        peer=peer,
        subject_user_id=link.local_user_id,
        link_id=link.id,
    )
    await db.commit()

    # Same cleanup as `revoke_my_link` and `revoke_peer`: requests in flight
    # are discarded at once. It is no longer the only protection -- the
    # responder asks the database for the link again before it reads, before
    # it stores the answer and on every poll (`_standing_refusal`) -- but it
    # frees the records without waiting for the next check.
    from services.federation_query_responder import purge_requests_for_pubkey
    discarded = await purge_requests_for_pubkey(peer.remote_pubkey)
    logger.info(
        f"🔗 Link {link_id} deleted by user {actor.id}; "
        f"{discarded} pending request(s) discarded"
    )
    return {"deleted": link_id}
