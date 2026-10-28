"""F-ID-3 gegen ECHTES Postgres — zwei Peers, zwei Stufen, keine Verwechslung.

Das Abnahmekriterium aus BL-0135, wörtlich:

> zwei Peers unter EINEM Eigentümer, beide `remote_user_id=1`, verschiedene
> Stufen → jeder bekommt seine eigene, kein `MultipleResultsFound`, keine
> fremde Gewährung.

🛑 Warum das gegen echtes Postgres laufen MUSS und nicht gegen sqlite:

1. Der Fehler, der F-ID-3 ausgelöst hat, war ein **Fremdschlüsselbruch**
   (`circle_memberships.member_user_id` → `users.id`). sqlite erzwingt
   Fremdschlüssel nicht; derselbe Fall wäre dort grün durchgelaufen, und zwar
   jahrelang — er ist es auch.
2. Die Zwei-Peer-Kollision war **latent**: sie braucht zwei echte Zeilen unter
   einem Eigentümer. Ein Formtest auf der SQL-Zeichenkette kann sie nicht
   zeigen, nur ein Lauf gegen Daten.

Gilt nur mit `RENFIELD_TEST_PG_URL` (sonst übersprungen).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import PeerUser, Role, User
from services.circle_sql import PeerScope, kg_entities_circles_filter

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.fixture
async def zwei_peers(pg_db_session: AsyncSession) -> dict:
    """EIN Eigentümer, ZWEI Peers — beide mit `remote_user_id = 1`.

    Genau die Lage, in der die alte Fassung scheiterte: beide Gegenstellen
    numerieren ihre Nutzer ab 1, also trafen beide dieselbe
    `circle_memberships`-Zeile.
    """
    role = Role(name="fid3-role", description="t", permissions=[])
    pg_db_session.add(role)
    await pg_db_session.flush()
    eigner = User(username="fid3-eigner", password_hash="x", is_active=True, role_id=role.id)
    fremder = User(username="fid3-fremder", password_hash="x", is_active=True, role_id=role.id)
    pg_db_session.add_all([eigner, fremder])
    await pg_db_session.flush()

    nah = PeerUser(
        circle_owner_id=eigner.id, remote_pubkey="aa" * 32,
        remote_display_name="naher Peer", remote_user_id=1,
        granted_tier=2, transport_config={},
    )
    fern = PeerUser(
        circle_owner_id=eigner.id, remote_pubkey="bb" * 32,
        remote_display_name="ferner Peer", remote_user_id=1,   # DIESELBE Zahl
        granted_tier=4, transport_config={},
    )
    pg_db_session.add_all([nah, fern])
    await pg_db_session.flush()
    return {"eigner": eigner.id, "fremder": fremder.id, "nah": nah, "fern": fern}


async def _sichtbar(db: AsyncSession, peer: PeerUser, stufen: list[int], eigner: int) -> set[int]:
    """Welche der gegebenen Stufen erreicht dieser Peer?

    Fragt den ECHTEN Filter gegen eine kleine Wertetabelle, statt eine
    Hilfstabelle anzulegen — es geht um die Klausel, nicht um `kg_entities`.
    """
    clause, params = kg_entities_circles_filter(
        0, alias="e", peer=PeerScope(owner_id=peer.circle_owner_id, tier=peer.granted_tier),
    )
    werte = ", ".join(f"({t}, {eigner})" for t in stufen)
    sql = text(f"SELECT e.circle_tier FROM (VALUES {werte}) AS e(circle_tier, user_id) WHERE {clause}")
    rows = (await db.execute(sql, params)).scalars().all()
    return set(rows)


async def test_two_peers_one_owner_keep_their_OWN_tier(pg_db_session, zwei_peers):
    """🛑 Das Abnahmekriterium. Vorher trafen beide dieselbe Mitgliedschaft."""
    eigner = zwei_peers["eigner"]
    stufen = [0, 1, 2, 3, 4]

    nah = await _sichtbar(pg_db_session, zwei_peers["nah"], stufen, eigner)
    fern = await _sichtbar(pg_db_session, zwei_peers["fern"], stufen, eigner)

    # Stufe 2 erreicht 2,3,4 — die Leiter laeuft gegen die Anschauung.
    assert nah == {2, 3, 4}, nah
    # Stufe 4 erreicht nur Oeffentliches.
    assert fern == {4}, fern
    # Und das ist der Kern: die beiden sehen NICHT dasselbe, obwohl ihre
    # `remote_user_id` identisch ist.
    assert nah != fern


async def test_a_peer_sees_NOTHING_of_another_owner(pg_db_session, zwei_peers):
    """🛑 Die Eigentuemer-Bindung. Ohne sie saehe der Peer seine Stufe bei
    JEDEM Eigentuemer der Instanz — aus der Reparatur wuerde ein Leck."""
    fremd = await _sichtbar(
        pg_db_session, zwei_peers["nah"], [0, 1, 2, 3], zwei_peers["fremder"])
    assert fremd == set(), fremd

    # Negativkontrolle: beim EIGENEN Eigentuemer sieht derselbe Peer sehr wohl
    # etwas — die leere Menge oben kommt also von der Bindung, nicht davon,
    # dass die Abfrage ueberhaupt nichts findet.
    eigen = await _sichtbar(
        pg_db_session, zwei_peers["nah"], [0, 1, 2, 3], zwei_peers["eigner"])
    assert eigen == {2, 3}, eigen


async def test_public_stays_reachable_for_any_owner(pg_db_session, zwei_peers):
    """Oeffentliches ist oeffentlich — auch bei einem fremden Eigentuemer.
    Das ist der zweite Zweig der Klausel und muss erhalten bleiben."""
    fremd = await _sichtbar(
        pg_db_session, zwei_peers["nah"], [3, 4], zwei_peers["fremder"])
    assert fremd == {4}, fremd


async def test_pairing_writes_NO_circle_membership_anymore(pg_db_session, zwei_peers):
    """🛑 Die Ursache, an der Datenbank gemessen.

    Bis 2026-10-27 schrieb jede Paarung eine `circle_memberships`-Zeile mit der
    ENTFERNTEN Nutzerkennung. Dass hier keine steht, ist kein Nebenbefund — es
    ist der ganze Fix.
    """
    n = (await pg_db_session.execute(text(
        "SELECT count(*) FROM circle_memberships WHERE circle_owner_id = :o"
    ), {"o": zwei_peers["eigner"]})).scalar()
    assert n == 0, f"{n} Mitgliedschaft(en) — die Paarung schreibt wieder in die Personentabelle"


async def test_the_tier_survives_a_round_trip_through_the_database(pg_db_session, zwei_peers):
    """Die Spalte ist `NOT NULL` mit Vorgabe 4. Eine neu angelegte Paarung ohne
    ausdrueckliche Stufe bekommt die ENGSTE — fail-closed, nicht grosszuegig."""
    ohne = PeerUser(
        circle_owner_id=zwei_peers["eigner"], remote_pubkey="cc" * 32,
        remote_display_name="ohne Angabe", remote_user_id=5, transport_config={},
    )
    pg_db_session.add(ohne)
    await pg_db_session.flush()
    await pg_db_session.refresh(ohne)
    assert ohne.granted_tier == 4, ohne.granted_tier

    sichtbar = await _sichtbar(pg_db_session, ohne, [0, 1, 2, 3, 4], zwei_peers["eigner"])
    assert sichtbar == {4}, sichtbar
