"""
Unit tests for AtomService — upsert + tier cascade + soft-delete.

Full integration (real Postgres with LEAST() + gen_random_uuid() +
IntegrityError-on-duplicate) is deferred to test_circles_v1_migration.py
which spins up a real pg. These tests verify:
  - upsert_atom source-row existence check, bind params, commit
  - update_tier cascade: atom policy rewrite + source circle_tier UPDATE
    + kg_node cascade to incident relations (when atom_type==kg_node)
  - soft_delete invokes the source table UPDATE + resolver invalidation

All tests @pytest.mark.unit — no network, no DB engine.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.atom_service import AtomService
from services.atom_types import Atom


def _mock_session():
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.rollback = AsyncMock()
    session.add = MagicMock()
    return session


def _existing_source_row(scalar=1):
    """Helper: db.execute return that scalar()-resolves to `scalar`."""
    res = MagicMock()
    res.scalar.return_value = scalar
    return res


def _new_atom(atom_type="kb_document", payload=None) -> Atom:
    return Atom(
        atom_id="",  # triggers UUID mint
        atom_type=atom_type,
        owner_user_id=42,
        policy={"tier": 2},
        payload=payload or {"chunk_id": 7},
        created_at=datetime.now(UTC).replace(tzinfo=None),
        updated_at=datetime.now(UTC).replace(tzinfo=None),
    )


class TestUpsertAtom:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_raises_when_source_row_missing(self):
        session = _mock_session()
        # First execute is the source-existence check → None means missing
        missing = MagicMock()
        missing.scalar.return_value = None
        session.execute = AsyncMock(return_value=missing)
        svc = AtomService(session)

        with pytest.raises(ValueError, match="does not exist"):
            await svc.upsert_atom(_new_atom())

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_inserts_when_no_existing_atom(self):
        session = _mock_session()
        # existence check → exists; select-existing-atom → None
        session.execute = AsyncMock(side_effect=[
            _existing_source_row(),            # SELECT 1 FROM source
            MagicMock(scalar_one_or_none=MagicMock(return_value=None)),  # SELECT atom
            MagicMock(),                       # UPDATE source.circle_tier
        ])
        svc = AtomService(session)
        atom_id = await svc.upsert_atom(_new_atom())

        assert atom_id
        session.add.assert_called_once()
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_updates_when_atom_exists(self):
        session = _mock_session()
        existing_atom = MagicMock()
        existing_atom.atom_id = "preexisting-uuid"
        session.execute = AsyncMock(side_effect=[
            _existing_source_row(),
            MagicMock(scalar_one_or_none=MagicMock(return_value=existing_atom)),
            MagicMock(),  # UPDATE source circle_tier
        ])
        svc = AtomService(session)
        atom_id = await svc.upsert_atom(_new_atom())

        assert atom_id == "preexisting-uuid"
        # No new atom added, existing row's policy updated in place.
        session.add.assert_not_called()
        assert existing_atom.policy == {"tier": 2}


class TestUpdateTier:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_noop_when_atom_missing(self):
        session = _mock_session()
        session.execute = AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
        ))
        svc = AtomService(session)
        # Missing atom is a warn+skip, not a raise.
        await svc.update_tier("no-such-id", {"tier": 3})
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_source_cascade_uses_text_cast_for_int_id_tables(self):
        """Regression for prod 500 on PATCH /api/atoms/{id}/tier.

        ``atom.source_id`` is TEXT (polymorphic across source tables),
        but most source tables (documents, kg_entities, memories...)
        have an INTEGER ``id`` column. asyncpg refuses to encode a
        Python str as int4 and the whole PATCH 500s. The fix: compare
        via ``id::text = :source_id`` so the SQL works regardless of
        the source table's id type.
        """
        session = _mock_session()
        atom_orm = MagicMock()
        atom_orm.atom_type = "kb_document"
        atom_orm.source_table = "documents"
        atom_orm.source_id = "99"
        # kb_document path: SELECT + generic-doc UPDATE + chunks + facts +
        # fact-atoms-policy + SELECT fact atom_ids (.fetchall() for invalidation)
        # + the edge cap UPDATE … RETURNING (#1625; no edges here)
        # + the contact-point cap and its recompute (#1240).
        fact_ids = MagicMock(fetchall=MagicMock(return_value=[]))
        no_edges = MagicMock(fetchall=MagicMock(return_value=[]))
        session.execute = AsyncMock(side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=atom_orm)),
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), fact_ids, no_edges,
            MagicMock(), MagicMock(),
        ])
        svc = AtomService(session, resolver=MagicMock(invalidate_for_atom=MagicMock()))
        await svc.update_tier("atom-x", {"tier": 3})

        cascade_sql = str(session.execute.await_args_list[1].args[0])
        assert "id::text = :source_id" in cascade_sql, (
            f"Generic source cascade must cast id to text. SQL was: {cascade_sql}"
        )
        # Bind param is passed through as-is (a str) — no callers need
        # to preconvert to int.
        binds = session.execute.await_args_list[1].args[1]
        assert binds == {"tier": 3, "source_id": "99"}

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_kb_document_cascades_to_document_chunks(self):
        """Post pc20260423: kb_document tier changes MUST propagate to every
        chunk's denormalized circle_tier in the same transaction — otherwise
        the hot-path SQL filter keeps reading stale chunk tiers and leaks
        chunks at the old tier (Risk A from docs/design/atoms-granularity.md).
        """
        session = _mock_session()
        atom_orm = MagicMock()
        atom_orm.atom_type = "kb_document"
        atom_orm.source_table = "documents"
        atom_orm.source_id = "99"
        fact_atoms_result = MagicMock()
        fact_atoms_result.fetchall = MagicMock(return_value=[
            MagicMock(atom_id="fact-atom-1"),
            MagicMock(atom_id="fact-atom-2"),
        ])
        edge_cap_result = MagicMock()
        edge_cap_result.fetchall = MagicMock(return_value=[
            MagicMock(id=5, atom_id="edge-atom-5"),
        ])
        session.execute = AsyncMock(side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=atom_orm)),
            MagicMock(),  # UPDATE documents.circle_tier
            MagicMock(),  # UPDATE document_chunks.circle_tier — cascade
            MagicMock(),  # UPDATE document_facts.circle_tier — Schicht A cascade
            MagicMock(),  # UPDATE atoms.policy for the fact atoms
            fact_atoms_result,  # SELECT atom_id FROM document_facts (for invalidation)
            edge_cap_result,  # UPDATE kg_relations tier_cap (#1625) RETURNING
            MagicMock(),  # UPDATE atoms.policy for the capped edges
            MagicMock(),  # UPDATE contact points source_tier cap (#1240)
            MagicMock(),  # UPDATE contact points circle_tier recompute (#1240)
        ])
        resolver = MagicMock(invalidate_for_atom=MagicMock())
        svc = AtomService(session, resolver=resolver)
        await svc.update_tier("atom-x", {"tier": 3})

        # 8 executes: SELECT atom + UPDATE documents + chunks + facts + fact-atoms
        # + SELECT fact atom_ids (for resolver-cache invalidation) + the edge cap
        # and its atom-policy sync (#1625) + the contact-point cap and its
        # recompute (#1240).
        assert session.execute.await_count == 10
        assert "kg_entity_contact_points" in str(session.execute.await_args_list[8].args[0])
        assert "kg_entity_contact_points" in str(session.execute.await_args_list[9].args[0])
        edge_sql = str(session.execute.await_args_list[6].args[0])
        assert "UPDATE kg_relations" in edge_sql and "tier_cap" in edge_sql
        assert session.execute.await_args_list[6].args[1] == {"tier": 3, "ref": "doc:99"}
        cascade_sql = str(session.execute.await_args_list[2].args[0])
        assert "UPDATE document_chunks" in cascade_sql
        assert "document_id = :doc_id" in cascade_sql
        binds = session.execute.await_args_list[2].args[1]
        assert binds == {"tier": 3, "doc_id": 99}
        # Schicht A facts inherit the document tier in the same transaction.
        facts_sql = str(session.execute.await_args_list[3].args[0])
        assert "UPDATE document_facts" in facts_sql
        assert "document_id = :doc_id" in facts_sql
        assert session.execute.await_args_list[3].args[1] == {"tier": 3, "doc_id": 99}
        # The fact-atom-id SELECT for invalidation.
        select_sql = str(session.execute.await_args_list[5].args[0])
        assert "SELECT atom_id FROM document_facts" in select_sql
        session.commit.assert_awaited_once()
        # Resolver cache invalidated for the parent doc atom AND each fact atom.
        invalidated = {c.args[0] for c in resolver.invalidate_for_atom.call_args_list}
        assert invalidated == {"atom-x", "fact-atom-1", "fact-atom-2", "edge-atom-5"}

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_kg_node_cascades_to_relations(self):
        session = _mock_session()
        atom_orm = MagicMock()
        atom_orm.atom_type = "kg_node"
        atom_orm.source_table = "kg_entities"
        atom_orm.source_id = "77"
        session.execute = AsyncMock(side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=atom_orm)),
            MagicMock(),  # UPDATE source.circle_tier
            MagicMock(),  # UPDATE kg_relations via LEAST()
            MagicMock(),  # UPDATE atoms.policy for edges
            MagicMock(),  # UPDATE contact points LEAST(entity, source) (#1240)
        ])
        svc = AtomService(session, resolver=MagicMock(invalidate_for_atom=MagicMock()))
        await svc.update_tier("kg-atom-7", {"tier": 2})

        # 5 executes: SELECT atom + UPDATE source + cascade + edge-policy sync
        # + the contact-point recompute (#1240).
        assert session.execute.await_count == 5
        assert "kg_entity_contact_points" in str(session.execute.await_args_list[4].args[0])
        # Verify the cascade SQL uses LEAST(s, o, cap) — the whole point (#1625).
        cascade_sql = str(session.execute.await_args_list[2].args[0])
        assert "LEAST(s.circle_tier, o.circle_tier, COALESCE(r.tier_cap" in cascade_sql
        # Verify the edge-policy sync SQL updates atoms table.
        edge_sync_sql = str(session.execute.await_args_list[3].args[0])
        assert "UPDATE atoms" in edge_sync_sql
        assert ":edge" in edge_sync_sql


class TestSoftDelete:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_marks_source_inactive(self):
        session = _mock_session()
        atom_orm = MagicMock()
        atom_orm.source_table = "conversation_memories"
        atom_orm.source_id = "42"
        session.execute = AsyncMock(side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=atom_orm)),
            MagicMock(),  # UPDATE is_active=false
        ])
        resolver = MagicMock(invalidate_for_atom=MagicMock())
        svc = AtomService(session, resolver=resolver)

        await svc.soft_delete("atom-1")

        assert session.execute.await_count == 2
        session.commit.assert_awaited_once()
        resolver.invalidate_for_atom.assert_called_once_with("atom-1")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_noop_when_atom_missing(self):
        session = _mock_session()
        session.execute = AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
        ))
        svc = AtomService(session)
        await svc.soft_delete("no-such-id")
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_rollback_if_source_update_errors(self):
        session = _mock_session()
        atom_orm = MagicMock()
        atom_orm.source_table = "kg_entities"
        atom_orm.source_id = "5"
        # First execute returns the atom; second (UPDATE) raises.
        session.execute = AsyncMock(side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=atom_orm)),
            Exception("no is_active column"),
        ])
        svc = AtomService(session, resolver=MagicMock(invalidate_for_atom=MagicMock()))
        # Should NOT raise — service handles the failure.
        await svc.soft_delete("atom-x")
        session.rollback.assert_awaited_once()
