"""F-ID-2 gegen ECHTES Postgres — die Wirkung, nicht nur der Ablauf.

Die Unit-Tests beweisen, dass der Handschlag die richtigen Aufrufe macht. Sie
koennen zwei Dinge nicht zeigen, auf die es ankommt:

1. **Dass die Verknuepfung wirkt.** `resolve_linked_user` ist die Funktion,
   die beim Antwortenden ueber `enforce_circles=False` entscheidet. Erst wenn
   sie nach dem Handschlag die Person liefert — und nach dem Widerruf wieder
   `None` —, ist die Kette geschlossen.
2. **Dass der Eindeutigkeitsindex traegt.** Er ist laut Modul-Docstring des
   Dienstes die EIGENTLICHE Sicherung gegen Wiedereinspielung, nicht der
   Nonce. Eine Behauptung ueber einen Index gehoert gegen die echte Bank
   geprueft; sqlite erzwingt sie anders.

Gilt nur mit `RENFIELD_TEST_PG_URL` (sonst uebersprungen).
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
    """🛑 Der Rueckweg wird DURCHLAUFEN, nicht behauptet.

    `resolve_linked_user` ist die Weiche beim Antwortenden. Vorher `None`
    (Rueckfall auf die Paarungsstufe), nach dem Handschlag die Person (volle
    eigene Reichweite), nach dem Widerruf wieder `None`. Ein Test, der nur den
    mittleren Zustand prueft, belegt nicht, dass der Widerruf greift.
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
    """🛑 Der Modul-Docstring des Dienstes sagt, der Index traegt — nicht der
    Nonce, der nur 60 s von 600 s abdeckt. Hier wird das nachgemessen statt
    geglaubt: dasselbe `(peer_id, querier_ref)` ein zweites Mal muss
    scheitern, auch mit einer ANDEREN Person.
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
            local_user_id=welt["fremde_id"],   # andere Person, gleiches Band
            querier_ref=ref, created_by=welt["fremde_id"],
        )
        await pg_db_session.flush()
    await pg_db_session.rollback()


async def test_a_deleted_person_demotes_the_link_instead_of_breaking_it(
    pg_db_session, welt,
):
    """Der Fremdschluessel steht auf `ON DELETE SET NULL`, und die Aufloesung
    behandelt `NULL` als unverknuepft. Das ist fail-closed: ein geloeschtes
    Konto fuehrt zum Rueckfall, nicht zu einem Treffer auf irgendwen.
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
