"""Two first uploads of one owner must end up in ONE inbox, and both succeed.

`get_or_create_owned_kb` looks the KB up by (name, owner) and inserts it when
missing. Two concurrent first uploads both miss the lookup and both INSERT;
`uq_knowledge_bases_name_owner` lets one through and the other used to raise
IntegrityError — the chat upload showed "Auto-index failed", the ingest push
answered 500 and retried. The loser must instead pick up the winner's row,
without poisoning its caller's transaction, and only for THAT index.

Real Postgres, real separate connections: the interleaving is forced, not
hoped for — the winner holds its INSERT uncommitted until the loser's INSERT is
seen waiting on it in `pg_stat_activity`.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from models.database import KnowledgeBase, SystemSetting
from services.ingest_common import get_or_create_owned_kb
from tests.backend.dbrows import ensure_role, ensure_user

pytestmark = [pytest.mark.backend, pytest.mark.database, pytest.mark.postgres]

OWNER = 7
NAME = "Eingang"


@pytest.fixture
async def make_session(pg_async_engine):
    maker = async_sessionmaker(pg_async_engine, expire_on_commit=False)
    async with maker() as db:
        await ensure_role(db)
        await ensure_user(db, OWNER)
        await db.commit()
    return maker


async def _wait_until_insert_blocks(make_session, timeout: float = 10.0) -> None:
    """Return once some backend's INSERT into knowledge_bases waits on a lock."""
    deadline = asyncio.get_running_loop().time() + timeout
    async with make_session() as probe:
        while True:
            waiting = (await probe.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' "
                "AND query ILIKE 'INSERT INTO knowledge_bases%'"
            ))).scalar_one()
            await probe.rollback()
            if waiting:
                return
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the second INSERT never blocked on the first")
            await asyncio.sleep(0.05)


async def _rows(make_session, owner):
    owner_clause = (
        KnowledgeBase.owner_id.is_(None) if owner is None else KnowledgeBase.owner_id == owner
    )
    async with make_session() as db:
        return list((await db.execute(
            select(KnowledgeBase.id).where(KnowledgeBase.name == NAME, owner_clause)
        )).scalars())


@pytest.mark.parametrize("owner", [OWNER, None], ids=["owned", "ownerless"])
async def test_the_loser_of_the_insert_race_gets_the_winners_kb(make_session, owner):
    async with make_session() as winner, make_session() as loser:
        # The winner's INSERT is in, uncommitted: invisible to the loser's
        # lookup, but it holds the unique key the loser's INSERT will want.
        won = KnowledgeBase(name=NAME, description="winner", owner_id=owner)
        winner.add(won)
        await winner.flush()

        # The caller's own pending work in the same transaction must survive.
        loser.add(SystemSetting(key="race.caller_work", value="kept"))
        race = asyncio.create_task(
            get_or_create_owned_kb(loser, NAME, owner, description="loser")
        )
        await _wait_until_insert_blocks(make_session)
        await winner.commit()

        got = await asyncio.wait_for(race, timeout=10)
        assert got.id == won.id
        assert got.description == "winner"

        # The session is still usable and the earlier work commits.
        await loser.commit()

    assert await _rows(make_session, owner) == [won.id]
    async with make_session() as db:
        kept = (await db.execute(
            select(SystemSetting.value).where(SystemSetting.key == "race.caller_work")
        )).scalar_one()
    assert kept == "kept"


async def test_many_concurrent_first_uploads_share_one_kb(make_session):
    async def first_upload():
        async with make_session() as db:
            return (await get_or_create_owned_kb(db, NAME, OWNER, description="d")).id

    ids = await asyncio.gather(*(first_upload() for _ in range(8)))
    assert len(set(ids)) == 1
    assert await _rows(make_session, OWNER) == [ids[0]]


async def test_an_unrelated_integrity_error_still_raises(make_session):
    """An owner that does not exist violates the FK, not the name index: that
    is a genuine error and must not be absorbed as "created concurrently"."""
    async with make_session() as db:
        with pytest.raises(IntegrityError) as exc:
            await get_or_create_owned_kb(db, NAME, 999_999, description="d")
        assert "uq_knowledge_bases_name_owner" not in str(exc.value)
        await db.rollback()
        count = (await db.execute(
            select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.name == NAME)
        )).scalar_one()
    assert count == 0
