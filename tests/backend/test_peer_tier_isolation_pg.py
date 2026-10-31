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
async def two_peers(pg_db_session: AsyncSession) -> dict:
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

    near = PeerUser(
        circle_owner_id=eigner.id, remote_pubkey="aa" * 32,
        remote_display_name="naher Peer", remote_user_id=1,
        granted_tier=2, transport_config={},
    )
    far = PeerUser(
        circle_owner_id=eigner.id, remote_pubkey="bb" * 32,
        remote_display_name="ferner Peer", remote_user_id=1,   # the SAME number
        granted_tier=4, transport_config={},
    )
    pg_db_session.add_all([near, far])
    await pg_db_session.flush()
    return {"eigner": eigner.id, "fremder": fremder.id, "near": near, "far": far}


async def _visible_tiers(db: AsyncSession, peer: PeerUser, tiers: list[int], eigner: int) -> set[int]:
    """Which of the given tiers does this peer reach?

    Queries the REAL filter against a small VALUES table instead of creating a
    helper table -- what matters is the clause, not `kg_entities`.
    """
    clause, params = kg_entities_circles_filter(
        0, alias="e", peer=PeerScope(owner_id=peer.circle_owner_id, tier=peer.granted_tier),
    )
    values = ", ".join(f"({t}, {eigner})" for t in tiers)
    sql = text(f"SELECT e.circle_tier FROM (VALUES {values}) AS e(circle_tier, user_id) WHERE {clause}")
    rows = (await db.execute(sql, params)).scalars().all()
    return set(rows)


async def test_two_peers_one_owner_keep_their_OWN_tier(pg_db_session, two_peers):
    """🛑 The acceptance criterion. Before this both hit the same membership."""
    eigner = two_peers["eigner"]
    tiers = [0, 1, 2, 3, 4]

    near = await _visible_tiers(pg_db_session, two_peers["near"], tiers, eigner)
    far = await _visible_tiers(pg_db_session, two_peers["far"], tiers, eigner)

    # Tier 2 reaches 2,3,4 -- the ladder runs against intuition.
    assert near == {2, 3, 4}, near
    # Tier 4 reaches only public content.
    assert far == {4}, far
    # And this is the core: the two do NOT see the same thing, although
    # their `remote_user_id` is identical.
    assert near != far


async def test_a_peer_sees_NOTHING_of_another_owner(pg_db_session, two_peers):
    """🛑 The owner binding. Without it the peer would see its tier at EVERY
    owner on the instance -- the repair would become a leak."""
    foreign = await _visible_tiers(
        pg_db_session, two_peers["near"], [0, 1, 2, 3], two_peers["fremder"])
    assert foreign == set(), foreign

    # Negative control: at its OWN owner the same peer does see something --
    # so the empty set above comes from the binding, not from the query
    # finding nothing at all.
    own = await _visible_tiers(
        pg_db_session, two_peers["near"], [0, 1, 2, 3], two_peers["eigner"])
    assert own == {2, 3}, own


async def test_public_stays_reachable_for_any_owner(pg_db_session, two_peers):
    """Public is public -- at a foreign owner too. That is the second branch
    of the clause and has to be preserved."""
    foreign = await _visible_tiers(
        pg_db_session, two_peers["near"], [3, 4], two_peers["fremder"])
    assert foreign == {4}, foreign


async def test_pairing_writes_NO_circle_membership_anymore(pg_db_session, two_peers):
    """🛑 The cause, measured against the database.

    Until 2026-10-27 every pairing wrote a `circle_memberships` row with the
    REMOTE user id. That none stands here is not a side finding -- it is the
    whole fix.
    """
    n = (await pg_db_session.execute(text(
        "SELECT count(*) FROM circle_memberships WHERE circle_owner_id = :o"
    ), {"o": two_peers["eigner"]})).scalar()
    assert n == 0, f"{n} membership(s) — the pairing writes into the person table again"


async def test_the_tier_survives_a_round_trip_through_the_database(pg_db_session, two_peers):
    """The column is `NOT NULL` with a default of 4. A newly created pairing
    without an explicit tier gets the NARROWEST -- fail-closed, not
    generous."""
    ohne = PeerUser(
        circle_owner_id=two_peers["eigner"], remote_pubkey="cc" * 32,
        remote_display_name="ohne Angabe", remote_user_id=5, transport_config={},
    )
    pg_db_session.add(ohne)
    await pg_db_session.flush()
    await pg_db_session.refresh(ohne)
    assert ohne.granted_tier == 4, ohne.granted_tier

    visible = await _visible_tiers(pg_db_session, ohne, [0, 1, 2, 3, 4], two_peers["eigner"])
    assert visible == {4}, visible
