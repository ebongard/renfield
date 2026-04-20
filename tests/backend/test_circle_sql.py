"""
Unit tests for circle_sql — the shared SQL filter clause builder used by
rag_retrieval, kg_retrieval, memory_retrieval. Pure string assembly tests;
no DB required.

What we verify:
- circles_filter_clause emits the 4-branch OR with correctly aliased columns
- params dict is shaped right
- per-table convenience wrappers wire `source_table` literal correctly
- document_chunks variant respects the cross-table owner alias (kb)
"""
from __future__ import annotations

from models.database import TIER_PUBLIC
from services.circle_sql import (
    circles_filter_clause,
    circles_filter_params,
    conversation_memories_circles_filter,
    document_chunks_circles_filter,
    kg_entities_circles_filter,
    kg_relations_circles_filter,
)


class TestCirclesFilterClause:
    def test_default_alias_owner_self_branch(self):
        clause = circles_filter_clause(table_alias="e")
        assert "e.user_id = :asker_id" in clause

    def test_default_alias_public_branch(self):
        clause = circles_filter_clause(table_alias="e")
        assert "e.circle_tier = :asker_id_pub" in clause

    def test_no_grant_subquery_when_source_table_empty(self):
        clause = circles_filter_clause(table_alias="e", source_table_value="")
        assert "atom_explicit_grants" not in clause

    def test_grant_subquery_present_when_source_table_set(self):
        clause = circles_filter_clause(table_alias="e", source_table_value="kg_entities")
        assert "atom_explicit_grants" in clause
        assert "a.source_table = :asker_id_src" in clause
        assert "a.source_id = (e.id)::text" in clause

    def test_membership_subquery_uses_owner_alias(self):
        clause = circles_filter_clause(table_alias="e")
        assert "circle_memberships m" in clause
        assert "m.circle_owner_id = e.user_id" in clause
        assert "m.dimension = 'tier'" in clause
        assert "(m.value)::int <= e.circle_tier" in clause

    def test_owner_table_alias_overrides_only_owner_col(self):
        # When owner is on a JOINed table (kb), tier should still come from
        # the main alias (dc).
        clause = circles_filter_clause(
            table_alias="dc",
            owner_col="owner_id",
            tier_col="circle_tier",
            source_table_value="document_chunks",
            owner_table_alias="kb",
        )
        assert "kb.owner_id = :asker_id" in clause
        assert "dc.circle_tier = :asker_id_pub" in clause
        assert "m.circle_owner_id = kb.owner_id" in clause
        assert "(m.value)::int <= dc.circle_tier" in clause

    def test_source_id_expr_overrides_default(self):
        clause = circles_filter_clause(
            table_alias="dc",
            owner_table_alias="kb",
            owner_col="owner_id",
            source_table_value="document_chunks",
            source_id_expr="dc.id",
        )
        assert "a.source_id = (dc.id)::text" in clause


class TestCirclesFilterParams:
    def test_default_params(self):
        params = circles_filter_params(asker_id=42)
        assert params == {"asker_id": 42, "asker_id_pub": TIER_PUBLIC}

    def test_custom_param_name(self):
        params = circles_filter_params(asker_id=7, asker_param="me")
        assert params == {"me": 7, "me_pub": TIER_PUBLIC}

    def test_source_table_value_emits_src_bind(self):
        params = circles_filter_params(asker_id=1, source_table_value="kg_entities")
        assert params["asker_id_src"] == "kg_entities"

    def test_no_src_bind_when_source_table_value_empty(self):
        params = circles_filter_params(asker_id=1, source_table_value="")
        assert "asker_id_src" not in params


class TestKgEntitiesWrapper:
    def test_returns_clause_and_params(self):
        clause, params = kg_entities_circles_filter(asker_id=42)
        assert "e.user_id = :asker_id" in clause
        assert "a.source_table = :asker_id_src" in clause
        assert params == {
            "asker_id": 42, "asker_id_pub": TIER_PUBLIC, "asker_id_src": "kg_entities",
        }

    def test_custom_alias(self):
        clause, _ = kg_entities_circles_filter(asker_id=1, alias="ent")
        assert "ent.user_id = :asker_id" in clause


class TestKgRelationsWrapper:
    def test_returns_clause_and_params(self):
        clause, params = kg_relations_circles_filter(asker_id=42)
        assert "r.user_id = :asker_id" in clause
        assert "a.source_table = :asker_id_src" in clause
        assert params == {
            "asker_id": 42, "asker_id_pub": TIER_PUBLIC, "asker_id_src": "kg_relations",
        }


class TestConversationMemoriesWrapper:
    def test_returns_clause_and_params(self):
        clause, params = conversation_memories_circles_filter(asker_id=42)
        assert "m.user_id = :asker_id" in clause
        assert "a.source_table = :asker_id_src" in clause
        assert params == {
            "asker_id": 42, "asker_id_pub": TIER_PUBLIC, "asker_id_src": "conversation_memories",
        }


class TestDocumentChunksWrapper:
    def test_owner_from_kb_tier_from_chunk(self):
        clause, params = document_chunks_circles_filter(asker_id=42)
        # Owner branch references kb.owner_id, not dc.user_id
        assert "kb.owner_id = :asker_id" in clause
        # Tier check is on chunk row
        assert "dc.circle_tier = :asker_id_pub" in clause
        # Membership reaches kb.owner_id
        assert "m.circle_owner_id = kb.owner_id" in clause
        # Grant subquery anchored on document_chunks (bound, not interpolated)
        assert "a.source_table = :asker_id_src" in clause
        assert "a.source_id = (dc.id)::text" in clause
        assert params == {
            "asker_id": 42, "asker_id_pub": TIER_PUBLIC, "asker_id_src": "document_chunks",
        }

    def test_custom_aliases(self):
        clause, _ = document_chunks_circles_filter(
            asker_id=42, chunk_alias="chunks", kb_alias="bases",
        )
        assert "bases.owner_id = :asker_id" in clause
        assert "chunks.circle_tier = :asker_id_pub" in clause
        assert "m.circle_owner_id = bases.owner_id" in clause
        assert "a.source_id = (chunks.id)::text" in clause
