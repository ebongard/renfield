"""Person-scoped federation — cross-instance identity link resolution (F-ID-1).

The `federation_user_links` table maps an opaque per-(peer, person)
``querier_ref`` token to a LOCAL user. The same rows serve both perspectives of
a pairing:

  * RESPONDER — ``resolve_link(peer_id, querier_ref) -> (link_id, local_user_id)``:
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

from models.database import FederationUserLink, PeerUser, User


def mint_querier_ref() -> str:
    """A fresh opaque per-(peer, person) token. 256-bit, hex — unguessable and
    not correlatable to any local id."""
    return secrets.token_hex(32)


async def resolve_link(
    db: AsyncSession, *, peer_id: int, querier_ref: str | None
) -> tuple[int, int] | None:
    """RESPONDER side: (peer_id, querier_ref) -> `(link_id, local_user_id)`,
    or None.

    None when: no ref, no matching row, the row's local_user_id was NULLed by
    a user delete, or the linked account is deactivated. Callers treat None as
    unmapped → peer-scoped fallback.

    The inner join on an ACTIVE account is the point: a link lends the far
    side that account's own reach, and a deactivated account has none -- it
    cannot sign in here either.

    The link id travels with the request (`_PendingRequest.link_id`) so that
    the answering side can ask again, while the query runs, whether THIS link
    still stands (`link_still_lends`).
    """
    if not querier_ref:
        return None
    row = (await db.execute(
        select(FederationUserLink.id, FederationUserLink.local_user_id)
        .join(User, User.id == FederationUserLink.local_user_id)
        .where(
            FederationUserLink.peer_id == peer_id,
            FederationUserLink.querier_ref == querier_ref,
            User.is_active.is_(True),
        )
    )).first()
    return (row[0], row[1]) if row is not None else None


async def resolve_linked_user(
    db: AsyncSession, *, peer_id: int, querier_ref: str | None
) -> int | None:
    """`resolve_link`, reduced to the local user id."""
    link = await resolve_link(db, peer_id=peer_id, querier_ref=querier_ref)
    return link[1] if link is not None else None


async def link_still_lends(
    db: AsyncSession, *, link_id: int, peer_id: int, local_user_id: int,
) -> bool:
    """Does the link a request was admitted on still lend this account's reach?

    The same conditions `resolve_link` applied, asked of the ROW it found: it
    still exists, still belongs to this pairing, still names this account, and
    the account is still active. A link deleted and created anew has another
    id -- consent given afresh does not reach back to a request in flight.
    """
    return (await db.execute(
        select(FederationUserLink.id)
        .join(User, User.id == FederationUserLink.local_user_id)
        .where(
            FederationUserLink.id == link_id,
            FederationUserLink.peer_id == peer_id,
            FederationUserLink.local_user_id == local_user_id,
            User.is_active.is_(True),
        )
    )).scalar_one_or_none() is not None


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


async def delete_links_for_peer(db: AsyncSession, *, peer_id: int) -> list[tuple[int, int | None]]:
    """Remove every person link of one pairing; returns `(link_id,
    local_user_id)` of each removed link.

    The pairing row is KEPT when it is revoked, and pairing again revives it
    under the same id -- link rows left behind would come back to life with it,
    and the far side, which still holds the `querier_ref`, would be served with
    a person's full reach that nobody consented to anew.
    """
    rows = (await db.execute(
        delete(FederationUserLink)
        .where(FederationUserLink.peer_id == peer_id)
        .returning(FederationUserLink.id, FederationUserLink.local_user_id)
    )).all()
    return sorted((row[0], row[1]) for row in rows)


async def revoke_links_of_pairing(
    db: AsyncSession, *, peer: PeerUser, actor_user_id: int, cause: str,
) -> int:
    """Delete the pairing's person links and record EACH one; returns how many.

    The one place both ends of a pairing's life go through: its revocation
    (`revoke_peer`) and the revival of a revoked row (`pairing_service`). The
    second matters for every pairing revoked before the first existed -- its
    links were left in place and would otherwise go live again on re-pairing.

    Most of these links belong to accounts other than the one acting, so a
    count is not a record: one `link_revoked` row per link, in the caller's
    transaction. Nothing is committed here.
    """
    from services.federation_audit import (
        BASIS_ADMIN,
        BASIS_OWNER,
        BASIS_SELF,
        LINK_REVOKED,
        record_revocation,
    )

    removed = await delete_links_for_peer(db, peer_id=peer.id)
    for link_id, local_user_id in removed:
        if local_user_id == actor_user_id:
            basis = BASIS_SELF
        elif peer.circle_owner_id == actor_user_id:
            basis = BASIS_OWNER
        else:
            basis = BASIS_ADMIN
        record_revocation(
            db,
            action=LINK_REVOKED,
            basis=basis,
            actor_user_id=actor_user_id,
            peer=peer,
            subject_user_id=local_user_id,
            link_id=link_id,
            cause=cause,
        )
    return len(removed)
