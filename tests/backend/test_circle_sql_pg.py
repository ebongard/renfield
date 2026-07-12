"""Postgres integration tests for the circle-SQL filter clause.

Closes the TD-20 gap (Reva-side `docs/technical-debt.md`): until now,
`circles_filter_clause()` was tested only via string-shape asserts in
`test_circle_sql.py` and via `test_circles_v1_services.py` running
against the SQLite in-memory fixture. SQLite is permissive about
`json → int` casts (it succeeds silently), so the production bug that
hit ebongard/renfield#545 — `(cm.value)::int` raising
`CannotCoerceError: cannot cast type json to integer` on asyncpg —
slipped through every existing test layer.

This module exercises the same code paths against a real Postgres
fixture (see `conftest.py::pg_db_session`), so any future regression
of the same shape fails at PR time instead of when a BaFin-regulated
tenant hits `/knowledge-graph` in production.

What's verified:

- The exact prod failure mode: legacy `(cm.value)::int` against a
  `JSON` column raises on Postgres; the canonical `(cm.value::text)::int`
  idiom (what `circle_sql.py` actually emits) succeeds.
- `circles_filter_clause` against `kg_entities`, `conversation_memories`
  end-to-end: real users, real `circle_memberships` rows with JSON
  values, real `atoms` + `atom_explicit_grants` rows. The right asker
  sees the right rows; no false positives, no `column does not exist`
  surprises from alias-shadow regressions.
- The `source_id` cast `(table.id)::text` works (int → text is trivial
  on PG; we still exercise the call site).
- `source_table_value` flows through a bind param — confirmed no JQL-
  like injection sink because the value is `:asker_id_src`, not an
  interpolated identifier.

Gated on `RENFIELD_TEST_PG_URL` via the `pg_db_session` fixture: skipped
when the env var is unset (so laptop sqlite-only runs are unaffected).
On the `.159` build box, the test container should set the env to the
build-box Postgres DSN.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    TIER_PUBLIC,
    Atom,
    AtomExplicitGrant,
    CircleMembership,
    ConversationMemory,
    Document,
    DocumentChunk,
    KGEntity,
    KnowledgeBase,
    Role,
    User,
)
from services.circle_sql import (
    conversation_memories_circles_filter,
    document_chunks_circles_filter,
    kg_entities_circles_filter,
)


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Fixture: seed a 4-user "household" plus a stranger, varying tier reach,
# and source rows (KGEntity + ConversationMemory) at every tier so each
# branch of the OR clause has at least one row to validate.
#
# Ladder reminder: tier 0 = self, 2 = household, 3 = extended, 4 = public.
# Member's circle_memberships.value is the deepest tier they can reach;
# the WHERE clause is `cm.value::int <= atom.circle_tier`.
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_circle_world(pg_db_session: AsyncSession) -> dict:
    """Seed a minimal but representative graph for circle-SQL tests.

    Returns a dict with the IDs that tests bind against so they don't
    need to re-query the seeded rows.
    """
    # Roles — pick the simplest non-Gast row (Owner) so the tests don't
    # accidentally pull on the WB person-filter Gast-exclusion path.
    role = Role(name="circle-pg-role", description="test", permissions=[])
    pg_db_session.add(role)
    await pg_db_session.flush()

    # Users — owner publishes atoms; member_household reaches tier ≥ 2;
    # member_extended reaches tier ≥ 3; stranger has no membership row.
    owner = User(
        username="circle-pg-owner",
        password_hash="x",
        is_active=True,
        role_id=role.id,
    )
    member_household = User(
        username="circle-pg-household",
        password_hash="x",
        is_active=True,
        role_id=role.id,
    )
    member_extended = User(
        username="circle-pg-extended",
        password_hash="x",
        is_active=True,
        role_id=role.id,
    )
    stranger = User(
        username="circle-pg-stranger",
        password_hash="x",
        is_active=True,
        role_id=role.id,
    )
    pg_db_session.add_all([owner, member_household, member_extended, stranger])
    await pg_db_session.flush()

    # Memberships — JSON column carries an int. This is the exact shape
    # that bit prod: `value` stored as integer JSON, then cast to int in
    # the WHERE clause.
    pg_db_session.add_all([
        CircleMembership(
            circle_owner_id=owner.id,
            member_user_id=member_household.id,
            dimension="tier",
            value=2,  # household reach
            granted_by=owner.id,
        ),
        CircleMembership(
            circle_owner_id=owner.id,
            member_user_id=member_extended.id,
            dimension="tier",
            value=3,  # extended reach
            granted_by=owner.id,
        ),
        # Also seed a NON-tier dimension to verify the dimension='tier'
        # filter in the EXISTS subquery doesn't accidentally include it.
        CircleMembership(
            circle_owner_id=owner.id,
            member_user_id=stranger.id,
            dimension="project",
            value="falcon",  # JSON string for set dimensions
            granted_by=owner.id,
        ),
    ])
    await pg_db_session.flush()

    # KG entities owned by `owner` at every tier so each WHERE branch
    # has at least one row.
    e_self = KGEntity(
        user_id=owner.id, name="ent-self", entity_type="thing",
        is_active=True, circle_tier=0,
    )
    e_household = KGEntity(
        user_id=owner.id, name="ent-household", entity_type="thing",
        is_active=True, circle_tier=2,
    )
    e_extended = KGEntity(
        user_id=owner.id, name="ent-extended", entity_type="thing",
        is_active=True, circle_tier=3,
    )
    e_public = KGEntity(
        user_id=owner.id, name="ent-public", entity_type="thing",
        is_active=True, circle_tier=TIER_PUBLIC,
    )
    pg_db_session.add_all([e_self, e_household, e_extended, e_public])
    await pg_db_session.flush()

    # Atom + explicit grant: stranger gets explicit access to ent-self
    # (tier 0). This exercises the explicit-grant branch.
    atom_self = Atom(
        atom_id="00000000-0000-0000-0000-00000000aaaa",
        atom_type="kg_node",
        source_table="kg_entities",
        source_id=str(e_self.id),
        owner_user_id=owner.id,
        policy={"tier": 0},
    )
    pg_db_session.add(atom_self)
    await pg_db_session.flush()
    pg_db_session.add(AtomExplicitGrant(
        atom_id=atom_self.atom_id,
        granted_to_user_id=stranger.id,
        permission_level="read",
        granted_by=owner.id,
    ))
    await pg_db_session.flush()

    # One conversation_memory owned by owner at tier=2 (household). The
    # alias-shadow regression specifically triggered on this table
    # because its default alias is "m" — same letter as the (former)
    # circle_memberships alias.
    mem_household = ConversationMemory(
        user_id=owner.id,
        content="household-memory",
        is_active=True,
        circle_tier=2,
    )
    pg_db_session.add(mem_household)
    await pg_db_session.flush()

    return {
        "owner_id": owner.id,
        "member_household_id": member_household.id,
        "member_extended_id": member_extended.id,
        "stranger_id": stranger.id,
        "kg_self_id": e_self.id,
        "kg_household_id": e_household.id,
        "kg_extended_id": e_extended.id,
        "kg_public_id": e_public.id,
        "mem_household_id": mem_household.id,
    }


# ---------------------------------------------------------------------------
# Direct cast tests — the exact prod failure mode from #545.
# ---------------------------------------------------------------------------


async def test_legacy_bare_json_to_int_cast_fails_on_postgres(
    pg_db_session: AsyncSession,
    seeded_circle_world: dict,
) -> None:
    """The bare `(cm.value)::int` cast that bit prod must still fail on PG.

    This is the canary that prevents a future "let's simplify the cast"
    refactor from regressing the fix. If this test ever starts passing,
    something has changed about how asyncpg coerces JSON — and the
    `(cm.value::text)::int` idiom in circle_sql.py needs review.
    """
    with pytest.raises(DBAPIError) as exc_info:
        await pg_db_session.execute(text(
            "SELECT (cm.value)::int FROM circle_memberships cm "
            "WHERE cm.dimension = 'tier' LIMIT 1"
        ))
    # The error wraps asyncpg's CannotCoerceError (PG SQLSTATE 42846).
    # Match on the substring rather than the exact class to stay
    # resilient against driver internal changes.
    msg = str(exc_info.value).lower()
    assert "json" in msg and "integer" in msg, (
        f"Expected json→integer cast error, got: {exc_info.value!r}"
    )


async def test_fixed_text_then_int_cast_succeeds_on_postgres(
    pg_db_session: AsyncSession,
    seeded_circle_world: dict,
) -> None:
    """The fixed `(cm.value::text)::int` idiom returns the seeded int values.

    Mirrors what `circles_filter_clause` emits today.
    """
    rows = (await pg_db_session.execute(text(
        "SELECT (cm.value::text)::int AS reach FROM circle_memberships cm "
        "WHERE cm.dimension = 'tier' ORDER BY reach"
    ))).all()
    reaches = [r.reach for r in rows]
    assert reaches == [2, 3], (
        f"Expected [2, 3] (household + extended), got {reaches}"
    )


# ---------------------------------------------------------------------------
# End-to-end clause tests — execute the actual SQL that production uses.
# ---------------------------------------------------------------------------


async def _run_kg_clause_for(
    session: AsyncSession, asker_id: int,
) -> set[int]:
    """Execute the kg_entities clause for `asker_id` and return visible IDs."""
    clause, params = kg_entities_circles_filter(asker_id=asker_id)
    sql = f"SELECT e.id FROM kg_entities e WHERE e.is_active AND ({clause})"
    result = await session.execute(text(sql), params)
    return {row.id for row in result.all()}


async def test_kg_entities_clause_owner_sees_everything(
    pg_db_session: AsyncSession,
    seeded_circle_world: dict,
) -> None:
    """Owner branch — `e.user_id = :asker_id` matches every row they own."""
    visible = await _run_kg_clause_for(pg_db_session, seeded_circle_world["owner_id"])
    expected = {
        seeded_circle_world["kg_self_id"],
        seeded_circle_world["kg_household_id"],
        seeded_circle_world["kg_extended_id"],
        seeded_circle_world["kg_public_id"],
    }
    assert visible == expected, (
        f"Owner must see all 4 seeded entities; missing={expected - visible}"
    )


async def test_kg_entities_clause_household_member_reach(
    pg_db_session: AsyncSession,
    seeded_circle_world: dict,
) -> None:
    """Member at tier=2 (household) sees household + extended + public — NOT self.

    The membership predicate is `cm.value <= e.circle_tier`. value=2 lets
    the member reach atoms at tier ≥ 2.
    """
    visible = await _run_kg_clause_for(
        pg_db_session, seeded_circle_world["member_household_id"],
    )
    assert seeded_circle_world["kg_self_id"] not in visible, (
        "household member at tier=2 must NOT reach owner's self atoms"
    )
    assert seeded_circle_world["kg_household_id"] in visible
    assert seeded_circle_world["kg_extended_id"] in visible
    assert seeded_circle_world["kg_public_id"] in visible


async def test_kg_entities_clause_extended_member_reach(
    pg_db_session: AsyncSession,
    seeded_circle_world: dict,
) -> None:
    """Member at tier=3 (extended) sees extended + public — NOT household, NOT self.

    The predicate `cm.value=3 <= e.circle_tier` is FALSE when circle_tier=2.
    Verifies the inequality direction is intact (not <= flipped to >=).
    """
    visible = await _run_kg_clause_for(
        pg_db_session, seeded_circle_world["member_extended_id"],
    )
    assert seeded_circle_world["kg_self_id"] not in visible
    assert seeded_circle_world["kg_household_id"] not in visible, (
        "extended member at tier=3 must NOT reach household-tier (2) atoms — "
        "the inequality is `value <= atom.circle_tier`, not the reverse"
    )
    assert seeded_circle_world["kg_extended_id"] in visible
    assert seeded_circle_world["kg_public_id"] in visible


async def test_kg_entities_clause_stranger_sees_only_public_plus_grants(
    pg_db_session: AsyncSession,
    seeded_circle_world: dict,
) -> None:
    """Stranger with no tier membership sees the public atom + the explicitly granted self atom.

    Validates the explicit-grant branch (`atom_explicit_grants`) and the
    public-tier branch — the only two paths a non-member can take.
    """
    visible = await _run_kg_clause_for(
        pg_db_session, seeded_circle_world["stranger_id"],
    )
    expected = {
        seeded_circle_world["kg_self_id"],      # via explicit grant
        seeded_circle_world["kg_public_id"],    # via public tier
    }
    assert visible == expected, (
        f"stranger should see only public + grant; got extra={visible - expected} "
        f"or missing={expected - visible}"
    )


async def test_kg_entities_clause_project_dimension_does_not_grant_tier_access(
    pg_db_session: AsyncSession,
    seeded_circle_world: dict,
) -> None:
    """A membership with dimension='project' must NOT satisfy the tier subquery.

    The seeded fixture gives `stranger` a dimension='project' membership.
    The EXISTS clause filters `cm.dimension = 'tier'`, so this row must
    not contribute to tier reach. If a future refactor drops that filter,
    this test catches it before it bleeds into prod.
    """
    visible = await _run_kg_clause_for(
        pg_db_session, seeded_circle_world["stranger_id"],
    )
    # Household tier (2) must NOT be visible — stranger's only membership
    # is dimension='project', which should be ignored by the tier path.
    assert seeded_circle_world["kg_household_id"] not in visible, (
        "dimension='project' membership leaked through the tier predicate"
    )


# ---------------------------------------------------------------------------
# Regression: alias-shadow bug on conversation_memories (alias 'm').
# Caught in prod 2026-05-12; the fix renamed the inner alias to 'cm'.
# ---------------------------------------------------------------------------


async def test_conversation_memories_clause_no_alias_shadow_on_postgres(
    pg_db_session: AsyncSession,
    seeded_circle_world: dict,
) -> None:
    """Execute the conversation_memories clause end-to-end against PG.

    Before the alias-shadow fix this exact query raised
    `column m.user_id does not exist` because the inner
    `circle_memberships m` masked the outer `conversation_memories m`.
    Running it here proves the rename to `cm` holds under real PG
    name resolution.
    """
    clause, params = conversation_memories_circles_filter(
        asker_id=seeded_circle_world["member_household_id"],
    )
    sql = (
        "SELECT m.id FROM conversation_memories m "
        f"WHERE m.is_active AND ({clause})"
    )
    result = await pg_db_session.execute(text(sql), params)
    ids = {row.id for row in result.all()}
    assert seeded_circle_world["mem_household_id"] in ids, (
        "household member at tier=2 must reach owner's tier=2 memory"
    )


# ---------------------------------------------------------------------------
# CM-1 regression: null-KB documents reach their owner via the atom-owner
# fallback. Global-RAG / KB-less documents have knowledge_bases.owner_id IS
# NULL after the LEFT JOIN, so the KB-owner branch can't fire — ownership
# must come from the document's atoms.owner_user_id instead. This was a
# latent gap in document_chunks retrieval (2/11 prod docs are null-KB,
# including the Schicht A doc-44 verification target).
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_document_world(pg_db_session: AsyncSession) -> dict:
    """Seed an owner with TWO self-tier (tier 0) documents + chunks:

    - a KB-owned document (knowledge_base_id set, kb.owner_id = owner)
    - a null-KB document (knowledge_base_id IS NULL, owner only on the atom)

    Plus a stranger with no access to either, to prove the atom-owner
    fallback doesn't leak self-tier null-KB content to non-owners.
    """
    role = Role(name="doc-pg-role", description="test", permissions=[])
    pg_db_session.add(role)
    await pg_db_session.flush()

    owner = User(username="doc-pg-owner", password_hash="x", is_active=True, role_id=role.id)
    stranger = User(username="doc-pg-stranger", password_hash="x", is_active=True, role_id=role.id)
    pg_db_session.add_all([owner, stranger])
    await pg_db_session.flush()

    kb = KnowledgeBase(name="doc-pg-kb", owner_id=owner.id, default_circle_tier=0)
    pg_db_session.add(kb)
    await pg_db_session.flush()

    # Atoms first — documents.atom_id is a FK into atoms. source_id is patched
    # after the documents flush (we don't know the doc ids yet); it isn't read
    # by the owner-branch fallback, only by the explicit-grant path.
    atom_kb_id = "00000000-0000-0000-0000-0000000d0001"
    atom_null_id = "00000000-0000-0000-0000-0000000d0002"
    # Distinct placeholder source_ids — uq_atoms_source spans
    # (atom_type, source_table, source_id), so they must differ before the
    # real document ids are patched in.
    atom_kb = Atom(
        atom_id=atom_kb_id, atom_type="kb_document",
        source_table="documents", source_id=atom_kb_id,
        owner_user_id=owner.id, policy={"tier": 0},
    )
    atom_null = Atom(
        atom_id=atom_null_id, atom_type="kb_document",
        source_table="documents", source_id=atom_null_id,
        owner_user_id=owner.id, policy={"tier": 0},
    )
    pg_db_session.add_all([atom_kb, atom_null])
    await pg_db_session.flush()

    # KB-owned document (control: visibility comes from kb.owner_id).
    doc_kb = Document(
        knowledge_base_id=kb.id, filename="kb.pdf", file_path="/x/kb.pdf",
        status="completed", circle_tier=0, atom_id=atom_kb_id,
    )
    # Null-KB document (the CM-1 case: kb.owner_id is NULL after LEFT JOIN).
    doc_null = Document(
        knowledge_base_id=None, filename="null.pdf", file_path="/x/null.pdf",
        status="completed", circle_tier=0, atom_id=atom_null_id,
    )
    pg_db_session.add_all([doc_kb, doc_null])
    await pg_db_session.flush()
    atom_kb.source_id = str(doc_kb.id)
    atom_null.source_id = str(doc_null.id)
    await pg_db_session.flush()

    chunk_kb = DocumentChunk(
        document_id=doc_kb.id, content="kb chunk", chunk_index=0, circle_tier=0,
    )
    chunk_null = DocumentChunk(
        document_id=doc_null.id, content="null-kb chunk", chunk_index=0, circle_tier=0,
    )
    pg_db_session.add_all([chunk_kb, chunk_null])
    await pg_db_session.flush()

    return {
        "owner_id": owner.id,
        "stranger_id": stranger.id,
        "chunk_kb_id": chunk_kb.id,
        "chunk_null_id": chunk_null.id,
    }


async def _run_chunk_clause_for(session: AsyncSession, asker_id: int) -> set[int]:
    """Execute the document_chunks clause for `asker_id`; return visible chunk IDs."""
    clause, params = document_chunks_circles_filter(asker_id=asker_id)
    sql = (
        "SELECT dc.id FROM document_chunks dc "
        "JOIN documents d ON dc.document_id = d.id "
        "LEFT JOIN knowledge_bases kb ON d.knowledge_base_id = kb.id "
        f"WHERE ({clause})"
    )
    result = await session.execute(text(sql), params)
    return {row.id for row in result.all()}


async def test_document_chunks_owner_sees_null_kb_chunk(
    pg_db_session: AsyncSession,
    seeded_document_world: dict,
) -> None:
    """CM-1: the owner reaches BOTH their KB-owned and their null-KB chunk.

    Before the atom-owner fallback, the null-KB chunk was invisible to its
    own owner (kb.owner_id IS NULL, tier 0 self → no public/grant/reach
    path). The fallback `EXISTS(atoms da … owner_user_id=:asker)` restores
    ownership.
    """
    visible = await _run_chunk_clause_for(pg_db_session, seeded_document_world["owner_id"])
    assert seeded_document_world["chunk_kb_id"] in visible, (
        "owner must still see their KB-owned chunk (no regression)"
    )
    assert seeded_document_world["chunk_null_id"] in visible, (
        "owner must now see their own null-KB chunk via the atom-owner fallback"
    )


async def test_document_chunks_stranger_sees_neither_self_chunk(
    pg_db_session: AsyncSession,
    seeded_document_world: dict,
) -> None:
    """The atom-owner fallback must not leak self-tier content to non-owners.

    A stranger owns neither atom and has no tier reach / grant, so both
    tier-0 chunks stay invisible — the fallback is owner-scoped.
    """
    visible = await _run_chunk_clause_for(pg_db_session, seeded_document_world["stranger_id"])
    assert seeded_document_world["chunk_kb_id"] not in visible
    assert seeded_document_world["chunk_null_id"] not in visible


# ---------------------------------------------------------------------------
# peer_scoped (federation) — the collision + explicit-grant suppression proof.
#
# A federated asker_id comes from PeerUser.remote_user_id, an FK-constrained
# local users.id that structurally collides with the owner's own id in a
# single-user household. peer_scoped MUST drop the owner-equality and
# explicit-grant branches so a colliding/granted asker sees ONLY public +
# pairing tier-membership.
# ---------------------------------------------------------------------------


async def _run_kg_clause_peer_scoped(session: AsyncSession, asker_id: int) -> set[int]:
    clause, params = kg_entities_circles_filter(asker_id=asker_id, peer_scoped=True)
    sql = f"SELECT e.id FROM kg_entities e WHERE e.is_active AND ({clause})"
    result = await session.execute(text(sql), params)
    return {row.id for row in result.all()}


async def test_peer_scoped_colliding_asker_id_denied_owner_branch(
    pg_db_session: AsyncSession,
    seeded_circle_world: dict,
) -> None:
    """THE core fix: asker_id == owner_id (the FK-forced collision) must NOT
    unlock the owner's own atoms under peer_scoped. With the owner branch
    dropped and no membership row for the owner-as-member, only public is left.
    """
    owner_id = seeded_circle_world["owner_id"]

    # Sanity: WITHOUT peer_scoped, that same id is the owner → sees everything.
    unscoped = await _run_kg_clause_for(pg_db_session, owner_id)
    assert seeded_circle_world["kg_self_id"] in unscoped

    # WITH peer_scoped: the colliding id sees ONLY public.
    visible = await _run_kg_clause_peer_scoped(pg_db_session, owner_id)
    assert seeded_circle_world["kg_self_id"] not in visible, (
        "peer_scoped must NOT let a colliding asker_id reach owner self atoms"
    )
    assert seeded_circle_world["kg_household_id"] not in visible
    assert seeded_circle_world["kg_extended_id"] not in visible
    assert visible == {seeded_circle_world["kg_public_id"]}


async def test_peer_scoped_drops_explicit_grant(
    pg_db_session: AsyncSession,
    seeded_circle_world: dict,
) -> None:
    """The explicit-grant branch is also an asker-id equality sink → dropped.

    `stranger` has an explicit grant to the tier-0 self entity. Unscoped they
    see it; peer_scoped they do not (only public remains).
    """
    stranger_id = seeded_circle_world["stranger_id"]

    unscoped = await _run_kg_clause_for(pg_db_session, stranger_id)
    assert seeded_circle_world["kg_self_id"] in unscoped, (
        "precondition: stranger sees self entity via explicit grant when unscoped"
    )

    visible = await _run_kg_clause_peer_scoped(pg_db_session, stranger_id)
    assert seeded_circle_world["kg_self_id"] not in visible, (
        "peer_scoped must drop the explicit-grant branch"
    )
    assert visible == {seeded_circle_world["kg_public_id"]}


async def test_peer_scoped_membership_still_reaches_granted_tier(
    pg_db_session: AsyncSession,
    seeded_circle_world: dict,
) -> None:
    """The safe branch survives: a peer granted tier=2 (household) still reaches
    household + extended + public via the pairing tier-membership EXISTS — and
    still NOT the owner's tier-0 self atom.
    """
    member_id = seeded_circle_world["member_household_id"]
    visible = await _run_kg_clause_peer_scoped(pg_db_session, member_id)
    assert seeded_circle_world["kg_self_id"] not in visible
    assert seeded_circle_world["kg_household_id"] in visible
    assert seeded_circle_world["kg_extended_id"] in visible
    assert seeded_circle_world["kg_public_id"] in visible
