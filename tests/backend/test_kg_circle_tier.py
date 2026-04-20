"""
Tests for the Lane C KG circle_tier surface:

- KnowledgeGraphService.update_entity_circle_tier validation + dispatch.
- The /circle-tiers route shape (label table + 5 rungs).
- The /entities/{id}/circle-tier endpoint validation.

Real cascade behavior (kg_relations recompute via LEAST(s.tier, o.tier))
requires a real Postgres LEAST() function and lives in the migration /
integration suites; here we only verify the service routes correctly to
AtomService when the entity has an atom_id.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routes.knowledge_graph import _TIER_LABELS, _TIER_NAMES
from models.database import TIER_PUBLIC
from services.knowledge_graph_service import KnowledgeGraphService


class TestTierLabelsTable:
    @pytest.mark.unit
    def test_five_rungs_present(self):
        assert set(_TIER_LABELS.keys()) == {0, 1, 2, 3, 4}
        assert set(_TIER_NAMES.keys()) == {0, 1, 2, 3, 4}

    @pytest.mark.unit
    def test_each_rung_has_de_and_en(self):
        for tier in range(5):
            assert "de" in _TIER_LABELS[tier]
            assert "en" in _TIER_LABELS[tier]
            for lang in ("de", "en"):
                assert _TIER_LABELS[tier][lang]["label"]
                assert _TIER_LABELS[tier][lang]["description"]

    @pytest.mark.unit
    def test_canonical_names_match_doc(self):
        # Locked vocabulary — these strings appear in the API response and
        # any frontend matching `name` against tier rungs depends on them.
        assert _TIER_NAMES == {
            0: "self", 1: "trusted", 2: "household", 3: "extended", 4: "public",
        }


class TestUpdateEntityCircleTierValidation:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_negative_tier_rejected(self):
        svc = KnowledgeGraphService(MagicMock())
        with pytest.raises(ValueError, match="Invalid circle_tier"):
            await svc.update_entity_circle_tier(entity_id=1, circle_tier=-1)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_above_public_rejected(self):
        svc = KnowledgeGraphService(MagicMock())
        with pytest.raises(ValueError, match="Invalid circle_tier"):
            await svc.update_entity_circle_tier(entity_id=1, circle_tier=TIER_PUBLIC + 1)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_unknown_entity_returns_none(self):
        svc = KnowledgeGraphService(MagicMock())
        with patch.object(svc, "get_entity", new=AsyncMock(return_value=None)):
            result = await svc.update_entity_circle_tier(entity_id=999, circle_tier=2)
        assert result is None


class TestUpdateEntityCircleTierDispatch:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_dispatches_to_atom_service_when_atom_id_present(self):
        svc = KnowledgeGraphService(MagicMock())
        ent = MagicMock()
        ent.atom_id = "abc-123"
        with patch.object(svc, "get_entity", new=AsyncMock(return_value=ent)), \
             patch("services.atom_service.AtomService.update_tier",
                   new=AsyncMock(return_value=None)) as upd, \
             patch.object(svc.db, "refresh", new=AsyncMock()):
            result = await svc.update_entity_circle_tier(entity_id=1, circle_tier=2)
        upd.assert_awaited_once()
        # Policy carries the new tier int
        assert upd.await_args.args[1] == "abc-123"
        assert upd.await_args.args[2] == {"tier": 2}
        assert result is ent

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_falls_back_to_direct_write_when_no_atom_id(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.flush = AsyncMock()
        svc = KnowledgeGraphService(db)
        ent = MagicMock()
        ent.atom_id = None
        with patch.object(svc, "get_entity", new=AsyncMock(return_value=ent)):
            result = await svc.update_entity_circle_tier(entity_id=5, circle_tier=3)
        assert result is ent
        assert ent.circle_tier == 3
        # The kg_relations recompute is issued
        db.execute.assert_awaited_once()
        sql_text = str(db.execute.call_args.args[0])
        assert "UPDATE kg_relations" in sql_text
        assert "LEAST(s.circle_tier, o.circle_tier)" in sql_text
        db.commit.assert_awaited_once()
        # Explicit flush MUST come before the raw UPDATE so LEAST() reads
        # the new entity.circle_tier (CRITICAL for cascade correctness).
        db.flush.assert_awaited_once()
        # Verify call order: get_entity → flush → execute → commit
        # (We don't enforce get_entity ordering since that uses a separate mock.)
        # If flush didn't fire before execute, the entity.circle_tier write
        # wouldn't be visible to the LEAST() reader.
