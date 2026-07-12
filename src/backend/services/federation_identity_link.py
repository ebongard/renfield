"""Person-scoped federation — cross-instance identity link resolution (F-ID-1).

The `federation_user_links` table maps an opaque per-(peer, person)
``querier_ref`` token to a LOCAL user. The same rows serve both perspectives of
a pairing:

  * RESPONDER — ``resolve_linked_user(peer_id, querier_ref) -> local_user_id``:
    an incoming federated query carrying ``querier_ref`` is served AS that local
    user (full circle reach), not the peer-scoped public/guest fallback.
  * ASKER — ``resolve_querier_ref(peer_id, local_user_id) -> querier_ref``: when
    that local user federates a query to the peer, the outgoing envelope carries
    the ref.

``querier_ref`` is a shared opaque token agreed at link time (F-ID-2 will mint it
via a consent handshake; F-ID-1 lets an admin assert it). It is NOT the raw
remote user id — see the multi-peer collision note in TODOS.md. Design:
docs/design/federation-identity-mapping.md.

All lookups are fail-closed: a missing row, a NULL ``local_user_id`` (owner
deleted, ON DELETE SET NULL), or a blank ref returns None → the caller falls
back to the peer-scoped path.
"""
from __future__ import annotations

import secrets

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import FederationUserLink


def mint_querier_ref() -> str:
    """A fresh opaque per-(peer, person) token. 256-bit, hex — unguessable and
    not correlatable to any local id."""
    return secrets.token_hex(32)


async def resolve_linked_user(
    db: AsyncSession, *, peer_id: int, querier_ref: str | None
) -> int | None:
    """RESPONDER side: (peer_id, querier_ref) -> local user id, or None.

    None when: no ref, no matching row, or the row's local_user_id was NULLed by
    a user delete. Callers treat None as unmapped → peer-scoped fallback.
    """
    if not querier_ref:
        return None
    row = (await db.execute(
        select(FederationUserLink.local_user_id).where(
            FederationUserLink.peer_id == peer_id,
            FederationUserLink.querier_ref == querier_ref,
        )
    )).scalar_one_or_none()
    return row  # int local_user_id, or None (no row / NULLed)


async def resolve_querier_ref(
    db: AsyncSession, *, peer_id: int, local_user_id: int | None
) -> str | None:
    """ASKER side: (peer_id, local_user_id) -> the ref to attach, or None.

    None when the querying user has no link for this peer — the asker then sends
    no querier_ref and the responder serves the fallback.
    """
    if local_user_id is None:
        return None
    # (peer_id, local_user_id) is NOT unique — only (peer_id, querier_ref) is —
    # so an admin MAY have created two refs for one (peer, user). Take the first
    # deterministically rather than raising MultipleResultsFound (which would
    # fail the query closed to fallback, but noisily).
    return (await db.execute(
        select(FederationUserLink.querier_ref).where(
            FederationUserLink.peer_id == peer_id,
            FederationUserLink.local_user_id == local_user_id,
        ).order_by(FederationUserLink.id).limit(1)
    )).scalars().first()


# ---------------------------------------------------------------------------
# Admin CRUD (F-ID-1 "admin assertion" link creation — F-ID-2 replaces this
# with a consent-signed double-login handshake).
# ---------------------------------------------------------------------------


async def create_link(
    db: AsyncSession, *, peer_id: int, local_user_id: int,
    querier_ref: str | None = None, created_by: int | None = None,
) -> FederationUserLink:
    """Create (or return existing) a link. ``querier_ref`` is minted if omitted.

    Idempotent on (peer_id, querier_ref): a duplicate ref for the same peer is a
    unique-constraint violation the caller surfaces as 409.
    """
    ref = querier_ref or mint_querier_ref()
    link = FederationUserLink(
        peer_id=peer_id, querier_ref=ref,
        local_user_id=local_user_id, created_by=created_by,
    )
    db.add(link)
    await db.flush()
    return link


async def list_links(db: AsyncSession, *, peer_id: int | None = None) -> list[FederationUserLink]:
    stmt = select(FederationUserLink)
    if peer_id is not None:
        stmt = stmt.where(FederationUserLink.peer_id == peer_id)
    return list((await db.execute(stmt.order_by(FederationUserLink.id))).scalars().all())


async def delete_link(db: AsyncSession, *, link_id: int) -> bool:
    res = await db.execute(delete(FederationUserLink).where(FederationUserLink.id == link_id))
    return res.rowcount > 0
