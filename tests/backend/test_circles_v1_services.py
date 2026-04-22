"""
Tests for circles v1 services: atom_types, CircleResolver, PolicyEvaluator,
PolymorphicAtomStore (RRF + result wrapping).

AtomService + the migration are tested separately:
  - tests/backend/test_circles_v1_migration.py for the alembic migration
    (fresh-DB upgrade + back-fill correctness).
  - AtomService.upsert_atom and update_tier need a real Postgres DB to test
    properly (atoms FK + JSON columns + LEAST() in cascade rule); deferred
    to integration tests run via `make test-integration` against a real
    backend container.

Coverage here (pure unit, no DB / Ollama / network):
- PolicyEvaluator: ladder + set + multi-dimension + missing-dimension
  fail-closed semantics.
- DimensionSpec.public_index correctness.
- Atom.tier convenience accessor.
- Provenance.redacted_for_remote: atom_id wiped, score rounded.
- _rrf_merge across heterogeneous source lists.
- _wrap_rag_results / _wrap_kg_context / _wrap_memory_results: result
  shape conversion + Exception guard (one source raising must not crash
  the whole query).
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from services.atom_types import (
    AccessContext,
    Atom,
    AtomMatch,
    DimensionSpec,
    Provenance,
)
from services.circle_resolver import PolicyEvaluator
from services.polymorphic_atom_store import (
    _rrf_merge,
    _wrap_kg_context,
    _wrap_memory_results,
    _wrap_rag_results,
)


def _atom(atom_id="x", policy=None, owner=42) -> Atom:
    # Use `is None` (not `or`) so an empty {} policy is preserved verbatim
    # for the test_tier_with_empty_policy case.
    if policy is None:
        policy = {"tier": 2}
    return Atom(
        atom_id=atom_id,
        atom_type="kb_document",
        owner_user_id=owner,
        policy=policy,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        updated_at=datetime.now(UTC).replace(tzinfo=None),
    )


# =============================================================================
# DimensionSpec
# =============================================================================


class TestDimensionSpec:
    @pytest.mark.unit
    def test_ladder_public_index_is_last(self):
        spec = DimensionSpec(shape="ladder", values=["self", "trusted", "household", "extended", "public"])
        assert spec.public_index == 4

    @pytest.mark.unit
    def test_ladder_with_two_values(self):
        spec = DimensionSpec(shape="ladder", values=["private", "public"])
        assert spec.public_index == 1

    @pytest.mark.unit
    def test_set_dimension_has_no_public_index(self):
        spec = DimensionSpec(shape="set", values=None)
        assert spec.public_index is None

    @pytest.mark.unit
    def test_ladder_without_values_has_no_public_index(self):
        spec = DimensionSpec(shape="ladder", values=None)
        assert spec.public_index is None


# =============================================================================
# Atom.tier convenience
# =============================================================================


class TestAtomTier:
    @pytest.mark.unit
    def test_tier_from_policy(self):
        assert _atom(policy={"tier": 3}).tier == 3

    @pytest.mark.unit
    def test_tier_defaults_to_zero_when_missing(self):
        assert _atom(policy={"tenant": "acme"}).tier == 0

    @pytest.mark.unit
    def test_tier_with_empty_policy(self):
        assert _atom(policy={}).tier == 0

    @pytest.mark.unit
    def test_from_mutable_deep_copies_policy(self):
        # Per PR #402 review OPTIONAL #17: from_mutable should isolate the
        # atom from later mutations of the source dict.
        source_policy = {"tier": 2}
        now = datetime.now(UTC).replace(tzinfo=None)
        atom = Atom.from_mutable(
            atom_id="x",
            atom_type="kb_document",
            owner_user_id=42,
            policy=source_policy,
            created_at=now,
            updated_at=now,
        )
        # Mutate the source dict AFTER construction — atom must not see it.
        source_policy["tier"] = 999
        assert atom.tier == 2

    @pytest.mark.unit
    def test_from_mutable_deep_copies_payload(self):
        source_payload = {"chunk_id": 7, "nested": {"key": "value"}}
        now = datetime.now(UTC).replace(tzinfo=None)
        atom = Atom.from_mutable(
            atom_id="x",
            atom_type="kb_document",
            owner_user_id=42,
            policy={"tier": 0},
            created_at=now,
            updated_at=now,
            payload=source_payload,
        )
        # Deep-copy means even nested dicts are isolated.
        source_payload["nested"]["key"] = "mutated"
        assert atom.payload["nested"]["key"] == "value"


# =============================================================================
# Provenance redaction
# =============================================================================


class TestProvenanceRedaction:
    @pytest.mark.unit
    def test_atom_id_wiped(self):
        # Per PR #402 review OPTIONAL #15: redaction now uses a random UUID4
        # per call (not a constant zero UUID) so receivers cannot dedupe atoms
        # across queries by atom_id. Verify the redacted ID is a valid UUID
        # and is NOT the original.
        import uuid as _uuid
        p = Provenance(atom_id="abc-123-real", atom_type="kb_document", display_label="from doc", score=0.85)
        redacted = p.redacted_for_remote()
        assert redacted.atom_id != p.atom_id
        # Should parse as a valid UUID4
        parsed = _uuid.UUID(redacted.atom_id)
        assert parsed.version == 4

    @pytest.mark.unit
    def test_atom_id_redaction_is_per_call(self):
        # Two calls should yield different redacted IDs (no cross-query correlation).
        p = Provenance(atom_id="x", atom_type="x", display_label="L", score=0.5)
        first = p.redacted_for_remote().atom_id
        second = p.redacted_for_remote().atom_id
        assert first != second

    @pytest.mark.unit
    def test_atom_type_preserved(self):
        p = Provenance(atom_id="x", atom_type="kg_node", display_label="L", score=0.5)
        assert p.redacted_for_remote().atom_type == "kg_node"

    @pytest.mark.unit
    def test_display_label_preserved(self):
        p = Provenance(atom_id="x", atom_type="x", display_label="Granny's recipes (2024-03)", score=0.5)
        assert p.redacted_for_remote().display_label == "Granny's recipes (2024-03)"

    @pytest.mark.unit
    def test_score_rounded_to_one_decimal(self):
        p = Provenance(atom_id="x", atom_type="x", display_label="L", score=0.847291)
        assert p.redacted_for_remote().score == 0.8

    @pytest.mark.unit
    def test_redaction_returns_new_instance(self):
        p = Provenance(atom_id="x", atom_type="x", display_label="L", score=0.5)
        assert p.redacted_for_remote() is not p


# =============================================================================
# PolicyEvaluator
# =============================================================================


_HOME_DIMS = {
    "tier": DimensionSpec(shape="ladder", values=["self", "trusted", "household", "extended", "public"]),
}

_ENTERPRISE_DIMS = {
    "tier": DimensionSpec(shape="ladder", values=["personal", "team", "org", "public"]),
    "tenant": DimensionSpec(shape="set"),
}


class TestPolicyEvaluator:
    """PolicyEvaluator.satisfies — the core access-policy algorithm."""

    @pytest.mark.unit
    def test_ladder_member_at_self_can_reach_self_atom(self):
        # Member placed at self (0) reaches atoms at tier 0 or wider.
        assert PolicyEvaluator.satisfies(
            atom_policy={"tier": 0},
            asker_memberships={"tier": 0},
            dimensions=_HOME_DIMS,
        )

    @pytest.mark.unit
    def test_ladder_member_at_household_can_reach_household_atom(self):
        assert PolicyEvaluator.satisfies(
            atom_policy={"tier": 2},
            asker_memberships={"tier": 2},
            dimensions=_HOME_DIMS,
        )

    @pytest.mark.unit
    def test_ladder_member_at_household_can_reach_public_atom(self):
        assert PolicyEvaluator.satisfies(
            atom_policy={"tier": 4},
            asker_memberships={"tier": 2},
            dimensions=_HOME_DIMS,
        )

    @pytest.mark.unit
    def test_ladder_member_at_household_blocked_from_self_atom(self):
        # Member placed at household (2) cannot reach atoms at tier 0 (self).
        assert not PolicyEvaluator.satisfies(
            atom_policy={"tier": 0},
            asker_memberships={"tier": 2},
            dimensions=_HOME_DIMS,
        )

    @pytest.mark.unit
    def test_ladder_member_at_extended_blocked_from_household_atom(self):
        # Member placed at extended (3) cannot reach atoms at household (2).
        assert not PolicyEvaluator.satisfies(
            atom_policy={"tier": 2},
            asker_memberships={"tier": 3},
            dimensions=_HOME_DIMS,
        )

    @pytest.mark.unit
    def test_set_dimension_exact_match_allows(self):
        assert PolicyEvaluator.satisfies(
            atom_policy={"tier": 1, "tenant": "acme"},
            asker_memberships={"tier": 1, "tenant": "acme"},
            dimensions=_ENTERPRISE_DIMS,
        )

    @pytest.mark.unit
    def test_set_dimension_mismatch_blocks(self):
        # Same tier but different tenant — must be blocked.
        assert not PolicyEvaluator.satisfies(
            atom_policy={"tier": 1, "tenant": "acme"},
            asker_memberships={"tier": 1, "tenant": "globex"},
            dimensions=_ENTERPRISE_DIMS,
        )

    @pytest.mark.unit
    def test_missing_dimension_in_membership_fails_closed(self):
        # Atom restricts on tenant; asker not in any tenant.
        assert not PolicyEvaluator.satisfies(
            atom_policy={"tier": 1, "tenant": "acme"},
            asker_memberships={"tier": 1},
            dimensions=_ENTERPRISE_DIMS,
        )

    @pytest.mark.unit
    def test_extra_membership_dimension_ignored(self):
        # Asker has tier+tenant, atom only restricts on tier — tenant ignored.
        assert PolicyEvaluator.satisfies(
            atom_policy={"tier": 2},
            asker_memberships={"tier": 1, "tenant": "anything"},
            dimensions=_ENTERPRISE_DIMS,
        )

    @pytest.mark.unit
    def test_unknown_dimension_in_atom_policy_fails_closed(self):
        # Atom references a dimension the deployment doesn't have configured.
        assert not PolicyEvaluator.satisfies(
            atom_policy={"tier": 4, "futuristic_dim": "value"},
            asker_memberships={"tier": 4, "futuristic_dim": "value"},
            dimensions=_HOME_DIMS,  # only 'tier' configured
        )

    @pytest.mark.unit
    def test_ladder_with_non_int_values_fails_closed(self):
        # Bad data in policy or membership: fail closed.
        assert not PolicyEvaluator.satisfies(
            atom_policy={"tier": "invalid_string"},
            asker_memberships={"tier": 2},
            dimensions=_HOME_DIMS,
        )

    @pytest.mark.unit
    def test_unknown_dimension_shape_fails_closed(self):
        weird_dims = {"tier": DimensionSpec(shape="not-a-real-shape")}
        assert not PolicyEvaluator.satisfies(
            atom_policy={"tier": 0},
            asker_memberships={"tier": 0},
            dimensions=weird_dims,
        )

    @pytest.mark.unit
    def test_empty_atom_policy_fails_closed(self):
        # Per PR #402 review BLOCKING #5: empty policy is treated as data corruption,
        # not "no restrictions". Returns False so atoms with bogus empty policies
        # don't become world-readable to anyone with any membership.
        assert not PolicyEvaluator.satisfies(
            atom_policy={},
            asker_memberships={"tier": 4},
            dimensions=_HOME_DIMS,
        )


# =============================================================================
# RRF merge
# =============================================================================


def _match(atom_id: str, rank: int) -> AtomMatch:
    return AtomMatch(
        atom=_atom(atom_id=atom_id),
        score=0.5,
        snippet=f"snippet {atom_id}",
        rank=rank,
    )


class TestRRFMerge:
    @pytest.mark.unit
    def test_empty_inputs_returns_empty(self):
        assert _rrf_merge([[], [], []], top_k=10) == []

    @pytest.mark.unit
    def test_single_source_passes_through(self):
        matches = [_match("a", 1), _match("b", 2), _match("c", 3)]
        result = _rrf_merge([matches], top_k=10)
        assert [m.atom.atom_id for m in result] == ["a", "b", "c"]

    @pytest.mark.unit
    def test_overlapping_ids_fuse_score(self):
        # 'a' appears in both sources at rank 1 — gets boosted score.
        # 'b' appears only in source 1; 'c' appears only in source 2.
        s1 = [_match("a", 1), _match("b", 2)]
        s2 = [_match("a", 1), _match("c", 2)]
        result = _rrf_merge([s1, s2], top_k=10, k=60)
        ids = [m.atom.atom_id for m in result]
        # 'a' should be ranked first (appears in both)
        assert ids[0] == "a"

    @pytest.mark.unit
    def test_top_k_truncates(self):
        matches = [_match(str(i), i) for i in range(1, 21)]
        result = _rrf_merge([matches], top_k=5)
        assert len(result) == 5

    @pytest.mark.unit
    def test_ranks_reassigned_post_merge(self):
        s1 = [_match("a", 1), _match("b", 2)]
        result = _rrf_merge([s1], top_k=10)
        assert [m.rank for m in result] == [1, 2]


# =============================================================================
# Source-result wrapping (Exception guards)
# =============================================================================


class TestSourceWrapping:
    @pytest.mark.unit
    def test_rag_wrapper_handles_exception_input(self):
        assert _wrap_rag_results(RuntimeError("retrieval blew up")) == []

    @pytest.mark.unit
    def test_rag_wrapper_handles_empty_input(self):
        assert _wrap_rag_results([]) == []
        assert _wrap_rag_results(None) == []

    @pytest.mark.unit
    def test_rag_wrapper_extracts_document_fields(self):
        """Post pc20260423: the atom id + owner live on the document, not
        on the chunk. Chunks contribute content/snippet + denormalized tier.
        """
        rag_results = [{
            "chunk": {
                "id": 7,
                "content": "The cat sat on the mat.",
                "page_number": 3,
                "section_title": "Pets",
                "circle_tier": 2,
            },
            "document": {
                "id": 1,
                "filename": "cats.pdf",
                "title": "Cat Lore",
                "atom_id": "doc-atom-1",
                "circle_tier": 2,
            },
            "similarity": 0.91,
        }]
        result = _wrap_rag_results(rag_results)
        assert len(result) == 1
        assert result[0].atom.atom_type == "kb_document"
        assert result[0].atom.atom_id == "doc-atom-1"
        assert result[0].atom.policy == {"tier": 2}
        assert result[0].score == 0.91
        assert "cat sat" in result[0].snippet
        assert result[0].rank == 1

    @pytest.mark.unit
    def test_rag_wrapper_falls_back_to_synthetic_atom_id_when_missing(self):
        # Defensive fallback when document.atom_id is absent (shouldn't happen
        # post-migration, but we log a warning and synthesize rather than crash).
        rag_results = [{
            "chunk": {"id": 42, "content": "x"},
            "document": {"id": 1, "filename": "y", "title": "z"},
            "similarity": 0.5,
        }]
        result = _wrap_rag_results(rag_results)
        assert result[0].atom.atom_id == "kb_document:1"
        assert result[0].atom.atom_type == "kb_document"

    @pytest.mark.unit
    def test_rag_wrapper_collapses_chunks_to_one_atom_per_document(self):
        """Post pc20260423: multiple chunk hits from the same document
        collapse to ONE AtomMatch carrying the best-scoring chunk's snippet.
        Without this, a single long document floods cross-source RRF with
        its own chunks and starves KG / memory results.
        """
        rag_results = [
            {
                "chunk": {"id": 1, "content": "first para", "circle_tier": 0},
                "document": {"id": 42, "filename": "a.pdf", "title": "A",
                             "atom_id": "doc-42", "circle_tier": 0},
                "similarity": 0.95,
            },
            {
                "chunk": {"id": 2, "content": "second para", "circle_tier": 0},
                "document": {"id": 42, "filename": "a.pdf", "title": "A",
                             "atom_id": "doc-42", "circle_tier": 0},
                "similarity": 0.80,
            },
            {
                "chunk": {"id": 3, "content": "B chunk", "circle_tier": 0},
                "document": {"id": 99, "filename": "b.pdf", "title": "B",
                             "atom_id": "doc-99", "circle_tier": 0},
                "similarity": 0.70,
            },
        ]
        result = _wrap_rag_results(rag_results)
        assert len(result) == 2
        # Best-scoring chunk wins for doc-42 — snippet from chunk id=1.
        doc42 = next(r for r in result if r.atom.atom_id == "doc-42")
        assert doc42.score == 0.95
        assert "first para" in doc42.snippet
        assert doc42.atom.payload["best_chunk_id"] == 1
        # Ranks reassigned sequentially after collapse.
        assert sorted(r.rank for r in result) == [1, 2]

    @pytest.mark.unit
    def test_kg_wrapper_handles_exception_input(self):
        assert _wrap_kg_context(RuntimeError("KG down")) == []

    @pytest.mark.unit
    def test_kg_wrapper_handles_none_input(self):
        assert _wrap_kg_context(None) == []
        assert _wrap_kg_context("") == []

    @pytest.mark.unit
    def test_kg_wrapper_returns_one_aggregated_match(self):
        kg_context = "WISSENSGRAPH:\n- Anna lives_in Hamburg\n- Anna born_in 1985"
        result = _wrap_kg_context(kg_context)
        assert len(result) == 1
        assert result[0].atom.atom_type == "kg_node"
        assert result[0].atom.atom_id == "kg_aggregated"
        assert "Anna" in result[0].atom.payload["content"]

    @pytest.mark.unit
    def test_memory_wrapper_handles_exception_input(self):
        assert _wrap_memory_results(RuntimeError("memory down")) == []

    @pytest.mark.unit
    def test_memory_wrapper_extracts_fields(self):
        memory_results = [{
            "id": 5,
            "content": "User prefers oat milk",
            "category": "preference",
            "importance": 0.85,
            "similarity": 0.78,
            "atom_id": "atom-5",
            "circle_tier": 0,
        }]
        result = _wrap_memory_results(memory_results)
        assert len(result) == 1
        assert result[0].atom.atom_type == "conversation_memory"
        assert result[0].atom.atom_id == "atom-5"
        assert result[0].atom.policy == {"tier": 0}
        assert result[0].score == 0.78


# =============================================================================
# AccessContext (dataclass shape)
# =============================================================================


class TestAccessContext:
    @pytest.mark.unit
    def test_construction(self):
        ctx = AccessContext(
            asker_id=42,
            dimensions=_HOME_DIMS,
            memberships={1: {"tier": 2}, 2: {"tier": 4}},
        )
        assert ctx.asker_id == 42
        assert ctx.dimensions["tier"].public_index == 4
        assert ctx.memberships[1] == {"tier": 2}
