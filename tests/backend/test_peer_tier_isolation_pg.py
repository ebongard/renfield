"""F-ID-3 against REAL Postgres -- two peers, two tiers, no mix-up.

The acceptance criterion from BL-0135, verbatim:

> two peers under ONE owner, both `remote_user_id=1`, different tiers -> each
> gets its own, no `MultipleResultsFound`, no foreign grant.

🛑 Why this MUST run against real Postgres and not against sqlite:

1. The defect that set off F-ID-3 was a **foreign-key break**
   (`circle_memberships.member_user_id` -> `users.id`). sqlite does not
   enforce foreign keys; the same case would have run green there, for years --
   and it did.
2. The two-peer collision was **latent**: it needs two real rows under one
   owner. A shape test on the SQL string cannot show it, only a run against
   data.

Applies only with `RENFIELD_TEST_PG_URL` (skipped otherwise).
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
    """ONE owner, TWO peers -- both with `remote_user_id = 1`.

    Exactly the situation the old version failed in: both far sides number
    their users from 1, so both hit the same `circle_memberships` row.
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
        remote_display_name="ferner Peer", remote_user_id=1,   # the SAME number
        granted_tier=4, transport_config={},
    )
    pg_db_session.add_all([nah, fern])
    await pg_db_session.flush()
    return {"eigner": eigner.id, "fremder": fremder.id, "nah": nah, "fern": fern}


async def _sichtbar(db: AsyncSession, peer: PeerUser, stufen: list[int], eigner: int) -> set[int]:
    """Which of the given tiers does this peer reach?

    Queries the REAL filter against a small VALUES table instead of creating a
    helper table -- what matters is the clause, not `kg_entities`.
    """
    clause, params = kg_entities_circles_filter(
        0, alias="e", peer=PeerScope(owner_id=peer.circle_owner_id, tier=peer.granted_tier),
    )
    werte = ", ".join(f"({t}, {eigner})" for t in stufen)
    sql = text(f"SELECT e.circle_tier FROM (VALUES {werte}) AS e(circle_tier, user_id) WHERE {clause}")
    rows = (await db.execute(sql, params)).scalars().all()
    return set(rows)


async def test_two_peers_one_owner_keep_their_OWN_tier(pg_db_session, zwei_peers):
    """🛑 The acceptance criterion. Before this both hit the same membership."""
    eigner = zwei_peers["eigner"]
    stufen = [0, 1, 2, 3, 4]

    nah = await _sichtbar(pg_db_session, zwei_peers["nah"], stufen, eigner)
    fern = await _sichtbar(pg_db_session, zwei_peers["fern"], stufen, eigner)

    # Tier 2 reaches 2,3,4 -- the ladder runs against intuition.
    assert nah == {2, 3, 4}, nah
    # Tier 4 reaches only public content.
    assert fern == {4}, fern
    # And this is the core: the two do NOT see the same thing, although
    # their `remote_user_id` is identical.
    assert nah != fern


async def test_a_peer_sees_NOTHING_of_another_owner(pg_db_session, zwei_peers):
    """🛑 The owner binding. Without it the peer would see its tier at EVERY
    owner on the instance -- the repair would become a leak."""
    fremd = await _sichtbar(
        pg_db_session, zwei_peers["nah"], [0, 1, 2, 3], zwei_peers["fremder"])
    assert fremd == set(), fremd

    # Negative control: at its OWN owner the same peer does see something --
    # so the empty set above comes from the binding, not from the query
    # finding nothing at all.
    eigen = await _sichtbar(
        pg_db_session, zwei_peers["nah"], [0, 1, 2, 3], zwei_peers["eigner"])
    assert eigen == {2, 3}, eigen


async def test_public_stays_reachable_for_any_owner(pg_db_session, zwei_peers):
    """Public is public -- at a foreign owner too. That is the second branch
    of the clause and has to be preserved."""
    fremd = await _sichtbar(
        pg_db_session, zwei_peers["nah"], [3, 4], zwei_peers["fremder"])
    assert fremd == {4}, fremd


async def test_pairing_writes_NO_circle_membership_anymore(pg_db_session, zwei_peers):
    """🛑 The cause, measured against the database.

    Until 2026-10-27 every pairing wrote a `circle_memberships` row with the
    REMOTE user id. That none stands here is not a side finding -- it is the
    whole fix.
    """
    n = (await pg_db_session.execute(text(
        "SELECT count(*) FROM circle_memberships WHERE circle_owner_id = :o"
    ), {"o": zwei_peers["eigner"]})).scalar()
    assert n == 0, f"{n} Mitgliedschaft(en) — die Paarung schreibt wieder in die Personentabelle"


async def test_the_tier_survives_a_round_trip_through_the_database(pg_db_session, zwei_peers):
    """The column is `NOT NULL` with a default of 4. A newly created pairing
    without an explicit tier gets the NARROWEST -- fail-closed, not
    generous."""
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
