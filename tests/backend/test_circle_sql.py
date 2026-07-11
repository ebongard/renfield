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
        # circle_memberships uses inner alias 'cm' (not 'm') to avoid
        # shadowing outer table aliases — see the rename commit message.
        assert "circle_memberships cm" in clause
        assert "cm.circle_owner_id = e.user_id" in clause
        assert "cm.dimension = 'tier'" in clause
        assert "(cm.value::text)::int <= e.circle_tier" in clause

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
        assert "cm.circle_owner_id = kb.owner_id" in clause
        assert "(cm.value::text)::int <= dc.circle_tier" in clause

    def test_source_id_expr_overrides_default(self):
        clause = circles_filter_clause(
            table_alias="dc",
            owner_table_alias="kb",
            owner_col="owner_id",
            source_table_value="document_chunks",
            source_id_expr="dc.id",
        )
        assert "a.source_id = (dc.id)::text" in clause

    def test_no_atom_owner_fallback_by_default(self):
        # Generic callers (kg_entities, conversation_memories) own via a
        # direct column — no atom-owner fallback should be emitted.
        clause = circles_filter_clause(table_alias="e")
        assert "atoms da" not in clause

    def test_atom_owner_fallback_when_owner_atom_id_expr_set(self):
        # CM-1: null-KB documents have kb.owner_id IS NULL, so the owner
        # branch must also match the document's atom owner.
        clause = circles_filter_clause(
            table_alias="dc",
            owner_col="owner_id",
            owner_table_alias="kb",
            source_table_value="documents",
            source_id_expr="d.id",
            owner_atom_id_expr="d.atom_id",
        )
        # The KB-owner branch and the atom-owner fallback are OR'd together.
        assert "kb.owner_id = :asker_id" in clause
        assert "FROM atoms da" in clause
        assert "da.atom_id = d.atom_id" in clause
        assert "da.owner_user_id = :asker_id" in clause
        # The fallback `da` alias must not collide with the grant `a` alias.
        assert "atoms a ON a.atom_id = g.atom_id" in clause


class TestPeerScoped:
    """peer_scoped=True (federation) MUST drop the owner-equality and
    explicit-grant branches — a federated asker_id originates from
    PeerUser.remote_user_id and can equal a real local owner id (the FK on
    circle_memberships.member_user_id forces it to a local users.id). Only the
    public-tier branch and the pairing tier-membership EXISTS are collision-safe.
    """

    def test_peer_scoped_omits_owner_branch(self):
        clause = circles_filter_clause(table_alias="e", source_table_value="kg_entities", peer_scoped=True)
        assert "e.user_id = :asker_id" not in clause

    def test_peer_scoped_omits_explicit_grant_branch(self):
        clause = circles_filter_clause(table_alias="e", source_table_value="kg_entities", peer_scoped=True)
        assert "atom_explicit_grants" not in clause

    def test_peer_scoped_omits_owner_branch_with_kb_alias(self):
        # Document path: owner is on the joined kb table — must also be dropped.
        clause = circles_filter_clause(
            table_alias="dc", owner_col="owner_id", source_table_value="documents",
            owner_table_alias="kb", source_id_expr="d.id", owner_atom_id_expr="d.atom_id",
            peer_scoped=True,
        )
        assert "kb.owner_id = :asker_id" not in clause
        assert "atom_explicit_grants" not in clause
        # The null-KB atom-owner fallback is part of the owner branch → also gone.
        assert "atoms da" not in clause

    def test_peer_scoped_keeps_public_and_membership_branches(self):
        clause = circles_filter_clause(table_alias="e", source_table_value="kg_entities", peer_scoped=True)
        assert "e.circle_tier = :asker_id_pub" in clause
        assert "circle_memberships cm" in clause
        assert "(cm.value::text)::int <= e.circle_tier" in clause

    def test_peer_scoped_clause_is_exactly_two_branches(self):
        clause = circles_filter_clause(table_alias="e", source_table_value="kg_entities", peer_scoped=True)
        # Provably: (public) OR (tier-membership EXISTS) — nothing else. The only
        # top-level OR joins the public branch directly to the membership EXISTS.
        assert clause.startswith("(") and clause.endswith(")")
        assert "e.circle_tier = :asker_id_pub OR EXISTS" in clause

    def test_default_is_byte_identical_to_no_kwarg(self):
        # Regression guard: omitting peer_scoped == peer_scoped=False == today.
        for src in ("", "kg_entities"):
            assert (
                circles_filter_clause(table_alias="e", source_table_value=src)
                == circles_filter_clause(table_alias="e", source_table_value=src, peer_scoped=False)
            )

    def test_wrappers_forward_peer_scoped(self):
        for wrapper in (
            kg_entities_circles_filter, kg_relations_circles_filter,
            conversation_memories_circles_filter,
        ):
            clause, _ = wrapper(42, peer_scoped=True)
            assert "atom_explicit_grants" not in clause
            assert "circle_memberships cm" in clause
        # document_chunks wrapper (kb-owner + atom-owner fallback both dropped)
        clause, _ = document_chunks_circles_filter(42, peer_scoped=True)
        assert "kb.owner_id = :asker_id" not in clause
        assert "atoms da" not in clause
        assert "atom_explicit_grants" not in clause


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

    def test_circle_memberships_alias_does_not_shadow_outer(self):
        """Regression: circle_memberships must NOT use alias 'm'.

        The default alias for conversation_memories is also 'm'. When the
        EXISTS subquery used 'circle_memberships m', the inner 'm'
        shadowed the outer wherever the clause referenced
        ``{owner_alias}.{owner_col}`` (which expands to 'm.user_id'). The
        production query then failed with "column m.user_id does not exist"
        because the resolution happened inside the subquery where 'm'
        means circle_memberships (no user_id column).

        Caught in prod 2026-05-12 in the chat_handler memory-retrieval
        path. Fix: alias circle_memberships as 'cm'.
        """
        clause, _ = conversation_memories_circles_filter(asker_id=42)
        assert "circle_memberships m " not in clause, (
            "circle_memberships must NOT use alias 'm' — would shadow "
            "the outer conversation_memories alias"
        )
        assert "circle_memberships cm " in clause


class TestDocumentChunksWrapper:
    def test_owner_from_kb_tier_from_chunk(self):
        """Post pc20260423 (atoms-per-document): the access-control unit is
        the parent Document, so the grant subquery anchors on ``documents``
        with source_id = d.id. Tier still comes from the chunk row (fast
        denormalized path — avoids the chunk→document JOIN in the hot
        similarity filter). Ownership still comes from kb.owner_id.
        """
        clause, params = document_chunks_circles_filter(asker_id=42)
        # Owner branch references kb.owner_id, not dc.user_id
        assert "kb.owner_id = :asker_id" in clause
        # CM-1: null-KB docs (kb.owner_id IS NULL) reach the owner via the
        # document's atom owner as a fallback.
        assert "FROM atoms da" in clause
        assert "da.atom_id = d.atom_id" in clause
        assert "da.owner_user_id = :asker_id" in clause
        # Tier check is on chunk row (denormalized)
        assert "dc.circle_tier = :asker_id_pub" in clause
        # Membership reaches kb.owner_id
        assert "m.circle_owner_id = kb.owner_id" in clause
        # Grant subquery anchored on documents (document-level atoms)
        assert "a.source_table = :asker_id_src" in clause
        assert "a.source_id = (d.id)::text" in clause
        assert params == {
            "asker_id": 42, "asker_id_pub": TIER_PUBLIC, "asker_id_src": "documents",
        }

    def test_custom_aliases(self):
        clause, _ = document_chunks_circles_filter(
            asker_id=42, chunk_alias="chunks", doc_alias="doc", kb_alias="bases",
        )
        assert "bases.owner_id = :asker_id" in clause
        assert "chunks.circle_tier = :asker_id_pub" in clause
        assert "m.circle_owner_id = bases.owner_id" in clause
        # Grant anchor follows the doc_alias now, not the chunk_alias.
        assert "a.source_id = (doc.id)::text" in clause
        # Atom-owner fallback follows the doc_alias too.
        assert "da.atom_id = doc.atom_id" in clause
