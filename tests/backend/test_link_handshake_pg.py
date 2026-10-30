"""F-ID-2 against REAL Postgres -- the effect, not just the sequence.

The unit tests prove that the handshake makes the right calls. There are two
things that matter which they cannot show:

1. **That the link takes effect.** `resolve_linked_user` is the function that
   decides `enforce_circles=False` on the answering side. Only when it returns
   the person after the handshake -- and `None` again after the revoke -- is
   the chain closed.
2. **That the uniqueness index holds.** According to the service's module
   docstring it is the REAL defence against a replay, not the nonce. An
   assertion about an index belongs checked against the real database; sqlite
   enforces it differently.

Applies only with `RENFIELD_TEST_PG_URL` (skipped otherwise).
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import PeerUser, Role, User
from services.federation_identity_link import (
    create_link,
    delete_link,
    mint_querier_ref,
    resolve_linked_user,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.fixture
async def welt(pg_db_session: AsyncSession) -> dict:
    role = Role(name="fid2-role", description="t", permissions=[])
    pg_db_session.add(role)
    await pg_db_session.flush()
    eigner = User(username="fid2-eigner", password_hash="x", is_active=True, role_id=role.id)
    person = User(username="fid2-person", password_hash="x", is_active=True, role_id=role.id)
    fremde = User(username="fid2-fremde", password_hash="x", is_active=True, role_id=role.id)
    pg_db_session.add_all([eigner, person, fremde])
    await pg_db_session.flush()
    peer = PeerUser(
        circle_owner_id=eigner.id, remote_pubkey="cd" * 32,
        remote_display_name="Gegenstelle", remote_user_id=777, transport_config={},
    )
    pg_db_session.add(peer)
    await pg_db_session.flush()
    return {
        "peer_id": peer.id, "eigner_id": eigner.id,
        "person_id": person.id, "fremde_id": fremde.id,
    }


async def test_the_link_DECIDES_the_mapped_path_and_the_revoke_undoes_it(
    pg_db_session, welt,
):
    """🛑 The way back is EXERCISED, not asserted.

    `resolve_linked_user` is the switch on the answering side. `None` before
    (falling back to the pairing tier), the person after the handshake (that
    person's own full reach), `None` again after the revoke. A test that checks
    only the middle state does not evidence that the revoke takes hold.
    """
    ref = mint_querier_ref()
    peer_id, person_id = welt["peer_id"], welt["person_id"]

    assert await resolve_linked_user(
        pg_db_session, peer_id=peer_id, querier_ref=ref) is None

    link = await create_link(
        pg_db_session, peer_id=peer_id, local_user_id=person_id,
        querier_ref=ref, created_by=person_id,
    )
    await pg_db_session.flush()
    assert await resolve_linked_user(
        pg_db_session, peer_id=peer_id, querier_ref=ref) == person_id

    assert await delete_link(pg_db_session, link_id=link.id) is True
    await pg_db_session.flush()
    assert await resolve_linked_user(
        pg_db_session, peer_id=peer_id, querier_ref=ref) is None


async def test_the_unique_index_IS_the_replay_defence(pg_db_session, welt):
    """🛑 The service's module docstring says the index holds -- not the
    nonce, which covers only 60 s out of 600 s. Here that is measured instead
    of believed: the same `(peer_id, querier_ref)` a second time has to fail,
    with a DIFFERENT person as well.
    """
    ref = mint_querier_ref()
    await create_link(
        pg_db_session, peer_id=welt["peer_id"], local_user_id=welt["person_id"],
        querier_ref=ref, created_by=welt["person_id"],
    )
    await pg_db_session.flush()

    with pytest.raises(IntegrityError):
        await create_link(
            pg_db_session, peer_id=welt["peer_id"],
            local_user_id=welt["fremde_id"],   # a different person, the same ref
            querier_ref=ref, created_by=welt["fremde_id"],
        )
        await pg_db_session.flush()
    await pg_db_session.rollback()


async def test_a_deleted_person_demotes_the_link_instead_of_breaking_it(
    pg_db_session, welt,
):
    """The foreign key is set to `ON DELETE SET NULL`, and the resolution
    treats `NULL` as unlinked. That is fail-closed: a deleted account leads to
    the fallback, not to a hit on just anybody.
    """
    from sqlalchemy import delete as sql_delete

    ref = mint_querier_ref()
    await create_link(
        pg_db_session, peer_id=welt["peer_id"], local_user_id=welt["fremde_id"],
        querier_ref=ref, created_by=welt["fremde_id"],
    )
    await pg_db_session.flush()
    assert await resolve_linked_user(
        pg_db_session, peer_id=welt["peer_id"], querier_ref=ref) == welt["fremde_id"]

    await pg_db_session.execute(
        sql_delete(User).where(User.id == welt["fremde_id"]))
    await pg_db_session.flush()

    assert await resolve_linked_user(
        pg_db_session, peer_id=welt["peer_id"], querier_ref=ref) is None
