"""The harness empties the database between tests by DELETE, not by TRUNCATE.

`TRUNCATE … CASCADE` on the tables a test wrote cost 2.2–2.5 s per database
test (#1577): two dirty tables dragged 66 of 79 into the CASCADE. The DELETE
path must leave exactly what the TRUNCATE left — every table empty, every
used sequence back at 1 — including the cases a single DELETE pass cannot
handle: a row trigger that fills another table, and a cycle of foreign keys.

The two `test_1…`/`test_2…` tests are a pair: the first writes, the second is
set up by the harness after it and checks what the harness left. pytest runs a
module's tests in file order.
"""

import pytest
from sqlalchemy import text

from models.database import Conversation, Message, Role, User
from tests.backend import conftest as harness
from tests.backend.conftest import (
    _EXISTING_TABLES_SQL,
    _delete_order,
    _empty_tables,
    _non_empty_tables_sql,
)

# --------------------------------------------------------------------------
# The order: children before parents, among the DIRTY tables only
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_children_come_before_their_parents():
    edges = [("users", "roles"), ("messages", "conversations"), ("conversations", "users")]
    order = _delete_order(["roles", "users", "conversations", "messages"], edges)
    assert order.index("messages") < order.index("conversations")
    assert order.index("conversations") < order.index("users")
    assert order.index("users") < order.index("roles")


@pytest.mark.unit
def test_an_empty_child_orders_nothing():
    # `users` is not dirty, so its edge to `roles` must not hold `roles` back.
    assert _delete_order(["roles"], [("users", "roles")]) == ["roles"]


@pytest.mark.unit
def test_a_cycle_between_dirty_tables_returns_none():
    assert _delete_order(["a", "b"], [("a", "b"), ("b", "a")]) is None


@pytest.mark.unit
def test_a_cycle_through_a_clean_table_is_no_cycle():
    assert _delete_order(["a"], [("a", "b"), ("b", "a")]) == ["a"]


# --------------------------------------------------------------------------
# The real database: what the next test finds
# --------------------------------------------------------------------------

_FALLBACKS_BEFORE: list[int] = []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_1_write_rows_a_trigger_and_a_rolled_back_insert(db_session):
    role = Role(name="cleanup-role", description="t", permissions=[])
    db_session.add(role)
    await db_session.flush()
    db_session.add(User(username="cleanup-user", password_hash="x", is_active=True, role_id=role.id))
    conv = Conversation(session_id="cleanup-session")
    db_session.add(conv)
    await db_session.flush()
    db_session.add(Message(conversation_id=conv.id, role="user", content="hallo"))
    await db_session.commit()
    # The conversation stays: the harness's own DELETE of it fires the
    # tombstone trigger (#1485) and fills `deleted_conversation_sessions`,
    # which a TRUNCATE never did -- the next test must find that table empty too.
    # A rolled-back insert advances a sequence and leaves no row.
    db_session.add(Role(name="never-committed", description="t", permissions=[]))
    await db_session.flush()
    await db_session.rollback()
    _FALLBACKS_BEFORE.append(harness._CLEANUP_COUNTS["truncate_fallback"])


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_2_the_next_test_finds_every_table_empty_and_ids_at_one(db_session):
    # Emptied by DELETE, not by the slow TRUNCATE fallback -- which would leave
    # the same empty database and hide that the fast path broke.
    assert harness._CLEANUP_COUNTS["truncate_fallback"] == _FALLBACKS_BEFORE[-1]
    tables = [r[0] for r in (await db_session.execute(text(_EXISTING_TABLES_SQL))).all()]
    dirty = [r[0] for r in (await db_session.execute(text(_non_empty_tables_sql(tables)))).all()]
    assert dirty == []
    role = Role(name="first", description="t", permissions=[])
    db_session.add(role)
    await db_session.flush()
    assert role.id == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conversations_and_messages_are_no_cycle(async_engine):
    """The active-leaf pointer (SET NULL) and `messages.conversation_id`
    (CASCADE) point at each other, but neither can refuse a DELETE. Counted as
    a cycle, they sent every chat test to the 2.4-s TRUNCATE."""
    from tests.backend.conftest import _FK_EDGES_SQL

    async with async_engine.connect() as conn:
        edges = [(r[0], r[1]) for r in (await conn.execute(text(_FK_EDGES_SQL))).all()]
    dirty = ["roles", "users", "conversations", "messages"]
    order = _delete_order(dirty, edges)
    assert order is not None
    assert order.index("conversations") < order.index("users") < order.index("roles")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_cycle_of_foreign_keys_still_ends_empty(async_engine):
    """No DELETE order exists for a cycle; the TRUNCATE fallback must run."""
    async with async_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE zz_cycle_a (id int PRIMARY KEY, b_id int)"))
        await conn.execute(text(
            "CREATE TABLE zz_cycle_b (id int PRIMARY KEY, a_id int REFERENCES zz_cycle_a(id))"
        ))
        await conn.execute(text(
            "ALTER TABLE zz_cycle_a ADD FOREIGN KEY (b_id) REFERENCES zz_cycle_b(id)"
        ))
        await conn.execute(text("INSERT INTO zz_cycle_a VALUES (1, NULL)"))
        await conn.execute(text("INSERT INTO zz_cycle_b VALUES (1, 1)"))
        await conn.execute(text("UPDATE zz_cycle_a SET b_id = 1"))
    try:
        async with async_engine.begin() as conn:
            await _empty_tables(conn, ["zz_cycle_a", "zz_cycle_b"])
            left = (await conn.execute(text(
                "SELECT (SELECT count(*) FROM zz_cycle_a) + (SELECT count(*) FROM zz_cycle_b)"
            ))).scalar()
        assert left == 0
    finally:
        async with async_engine.begin() as conn:
            await conn.execute(text("DROP TABLE zz_cycle_a, zz_cycle_b CASCADE"))
