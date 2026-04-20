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
from unittest.mock import AsyncMock, MagicMock, patch

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


def _new_atom(atom_type="kb_chunk", payload=None) -> Atom:
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
    async def test_kb_chunk_writes_source_tier_only(self):
        session = _mock_session()
        atom_orm = MagicMock()
        atom_orm.atom_type = "kb_chunk"
        atom_orm.source_table = "document_chunks"
        atom_orm.source_id = "99"
        session.execute = AsyncMock(side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=atom_orm)),
            MagicMock(),  # UPDATE source.circle_tier
        ])
        svc = AtomService(session, resolver=MagicMock(invalidate_for_atom=MagicMock()))
        await svc.update_tier("atom-x", {"tier": 3})

        # 2 executes: SELECT atom, UPDATE source. No cascade for kb_chunk.
        assert session.execute.await_count == 2
        session.commit.assert_awaited_once()

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
        ])
        svc = AtomService(session, resolver=MagicMock(invalidate_for_atom=MagicMock()))
        await svc.update_tier("kg-atom-7", {"tier": 2})

        # 4 executes: SELECT atom + UPDATE source + cascade + edge-policy sync.
        assert session.execute.await_count == 4
        # Verify the cascade SQL uses LEAST(s, o) — the whole point.
        cascade_sql = str(session.execute.await_args_list[2].args[0])
        assert "LEAST(s.circle_tier, o.circle_tier)" in cascade_sql
        # Verify the edge-policy sync SQL updates atoms table.
        edge_sync_sql = str(session.execute.await_args_list[3].args[0])
        assert "UPDATE atoms" in edge_sync_sql
        assert "kg_edge" in edge_sync_sql.lower() or ":edge_type" in edge_sync_sql


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
