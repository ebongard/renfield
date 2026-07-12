"""Postgres-gated round-trip for the federation identity-link service (F-ID-1).

Proves the real SQL: create a link, resolve it both ways (responder ref->user,
asker user->ref), and confirm fail-closed on a missing ref / user. Gated on
`RENFIELD_TEST_PG_URL` via `pg_db_session` (skipped on sqlite-only laptop runs).
Design: docs/design/federation-identity-mapping.md.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import PeerUser, Role, User
from services.federation_identity_link import (
    create_link,
    delete_link,
    mint_querier_ref,
    resolve_linked_user,
    resolve_querier_ref,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.fixture
async def linked_world(pg_db_session: AsyncSession) -> dict:
    role = Role(name="fed-link-role", description="t", permissions=[])
    pg_db_session.add(role)
    await pg_db_session.flush()
    owner = User(username="fed-link-owner", password_hash="x", is_active=True, role_id=role.id)
    person = User(username="fed-link-person", password_hash="x", is_active=True, role_id=role.id)
    pg_db_session.add_all([owner, person])
    await pg_db_session.flush()
    peer = PeerUser(
        circle_owner_id=owner.id, remote_pubkey="ab" * 32,
        remote_display_name="peer", remote_user_id=555, transport_config={},
    )
    pg_db_session.add(peer)
    await pg_db_session.flush()
    return {"peer_id": peer.id, "owner_id": owner.id, "person_id": person.id}


async def test_create_and_resolve_both_directions(pg_db_session, linked_world):
    peer_id = linked_world["peer_id"]
    person_id = linked_world["person_id"]
    ref = mint_querier_ref()

    link = await create_link(
        pg_db_session, peer_id=peer_id, local_user_id=person_id,
        querier_ref=ref, created_by=linked_world["owner_id"],
    )
    await pg_db_session.flush()
    assert link.querier_ref == ref

    # Responder direction: ref -> local user.
    assert await resolve_linked_user(pg_db_session, peer_id=peer_id, querier_ref=ref) == person_id
    # Asker direction: local user -> ref.
    assert await resolve_querier_ref(pg_db_session, peer_id=peer_id, local_user_id=person_id) == ref


async def test_resolve_fail_closed(pg_db_session, linked_world):
    peer_id = linked_world["peer_id"]
    # No ref / unknown ref / unknown user → None (fallback).
    assert await resolve_linked_user(pg_db_session, peer_id=peer_id, querier_ref=None) is None
    assert await resolve_linked_user(pg_db_session, peer_id=peer_id, querier_ref="nope") is None
    assert await resolve_querier_ref(pg_db_session, peer_id=peer_id, local_user_id=None) is None
    assert await resolve_querier_ref(pg_db_session, peer_id=peer_id, local_user_id=999999) is None


async def test_mint_is_unique_and_opaque(pg_db_session, linked_world):
    # 256-bit random hex: unique per call, fixed length, hex charset. (A
    # substring check against a short numeric id is meaningless here — single
    # decimal digits appear in 64-char random hex ~99% of the time; opaqueness
    # is proven by "not derived from the id at all" = randomness + uniqueness.)
    mints = {mint_querier_ref() for _ in range(50)}
    assert len(mints) == 50                       # all unique
    for m in mints:
        assert len(m) == 64                       # 32 bytes hex
        assert all(c in "0123456789abcdef" for c in m)


async def test_delete_reverts_to_fallback(pg_db_session, linked_world):
    peer_id = linked_world["peer_id"]
    person_id = linked_world["person_id"]
    ref = mint_querier_ref()
    link = await create_link(pg_db_session, peer_id=peer_id, local_user_id=person_id, querier_ref=ref)
    await pg_db_session.flush()

    assert await delete_link(pg_db_session, link_id=link.id) is True
    await pg_db_session.flush()
    assert await resolve_linked_user(pg_db_session, peer_id=peer_id, querier_ref=ref) is None
    # Deleting a non-existent link is a no-op False.
    assert await delete_link(pg_db_session, link_id=link.id) is False
