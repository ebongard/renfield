"""
Federation identity-link admin API (F-ID-1 — person-scoped federation).

ADMIN-gated CRUD over `federation_user_links` — the cross-instance person map
that lets a federated query be served AS a mapped local user (full circle
reach) instead of the peer-scoped public/guest fallback.

This is the F-ID-1 "admin assertion" way to create links (design doc §4.2
option B). F-ID-2 replaces it with a consent-signed double-login handshake that
proves ownership of both accounts. Until then, only an ADMIN may assert a link.

The `querier_ref` is the shared opaque token both sides of a pairing must hold:
- On the RESPONDER instance, the link means "ref X → my local user Y".
- On the ASKER instance, the link means "my local user Y, querying this peer,
  presents ref X".
So to make a person's mapping work end-to-end, the SAME (peer, ref, local_user)
triple is created on both instances (with each side's own local_user_id).
Create on one side without a ref to mint one, then reuse that ref on the other.

Design: docs/design/federation-identity-mapping.md.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import FederationUserLink, PeerUser, User
from models.permissions import Permission
from services.auth_service import require_permission
from services.database import get_db
from services.federation_identity_link import (
    create_link,
    delete_link,
    list_links,
)

router = APIRouter()


class CreateLinkRequest(BaseModel):
    peer_id: int
    local_user_id: int
    # Omit to MINT a fresh ref (first side of a pairing); pass the minted ref
    # when creating the mirror link on the other instance. Bounded to the DB
    # column width (String(128)) so an oversized ref is a clean 422, not a 500
    # DataError the IntegrityError handler wouldn't catch.
    querier_ref: str | None = Field(default=None, max_length=128)


class LinkResponse(BaseModel):
    id: int
    peer_id: int
    querier_ref: str
    local_user_id: int | None
    created_by: int | None


def _to_response(link: FederationUserLink) -> LinkResponse:
    return LinkResponse(
        id=link.id, peer_id=link.peer_id, querier_ref=link.querier_ref,
        local_user_id=link.local_user_id, created_by=link.created_by,
    )


@router.get("/user-links", response_model=list[LinkResponse])
async def list_user_links(
    request: Request,
    peer_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
) -> list[LinkResponse]:
    """List identity links (optionally filtered by peer). The `querier_ref` is
    returned so an admin can mirror it to the other instance."""
    links = await list_links(db, peer_id=peer_id)
    return [_to_response(link) for link in links]


@router.post("/user-links", response_model=LinkResponse)
async def create_user_link(
    request: Request,
    body: CreateLinkRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_permission(Permission.ADMIN)),
) -> LinkResponse:
    """Assert a person link. Validates the peer + local user exist. 409 if a link
    already exists for (peer, querier_ref). Links may be created while the feature
    is dark (`federation_identity_links_enabled=False`); they take effect only
    once it's enabled."""
    peer = (await db.execute(
        select(PeerUser).where(PeerUser.id == body.peer_id, PeerUser.revoked_at.is_(None))
    )).scalar_one_or_none()
    if peer is None:
        raise HTTPException(status_code=404, detail="Unknown or revoked peer")

    user = (await db.execute(
        select(User).where(User.id == body.local_user_id)
    )).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Unknown local user")

    try:
        link = await create_link(
            db, peer_id=body.peer_id, local_user_id=body.local_user_id,
            querier_ref=body.querier_ref,
            created_by=(admin.id if admin is not None else None),
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Link already exists for this (peer, querier_ref)")
    return _to_response(link)


@router.delete("/user-links/{link_id}")
async def delete_user_link(
    request: Request,
    link_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
) -> dict:
    """Revoke a person link → that person reverts to the peer-scoped fallback."""
    ok = await delete_link(db, link_id=link_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Link not found")
    await db.commit()
    return {"deleted": link_id}
