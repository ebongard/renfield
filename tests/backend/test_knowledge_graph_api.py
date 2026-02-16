"""
Tests for Knowledge Graph API — Pydantic schemas and route validation.

Tests focus on schema validation and mocked service layer.
"""
import pytest
from pydantic import ValidationError

from api.routes.knowledge_graph_schemas import (
    EntityBrief,
    EntityListResponse,
    EntityResponse,
    EntityUpdate,
    KGStatsResponse,
    MergeEntitiesRequest,
    RelationListResponse,
    RelationResponse,
)

# ==========================================================================
# Schema Tests
# ==========================================================================

class TestEntityResponseSchema:
    @pytest.mark.unit
    def test_valid_entity_response(self):
        resp = EntityResponse(
            id=1,
            name="Edi",
            entity_type="person",
            description="A developer",
            mention_count=5,
            first_seen_at="2026-01-01T00:00:00",
            last_seen_at="2026-02-15T12:00:00",
        )
        assert resp.id == 1
        assert resp.name == "Edi"
        assert resp.entity_type == "person"
        assert resp.mention_count == 5

    @pytest.mark.unit
    def test_entity_response_defaults(self):
        resp = EntityResponse(id=1, name="X", entity_type="thing")
        assert resp.description is None
        assert resp.mention_count == 1
        assert resp.first_seen_at == ""
        assert resp.last_seen_at == ""


class TestEntityUpdateSchema:
    @pytest.mark.unit
    def test_all_fields_optional(self):
        update = EntityUpdate()
        assert update.name is None
        assert update.entity_type is None
        assert update.description is None

    @pytest.mark.unit
    def test_partial_update(self):
        update = EntityUpdate(name="New Name")
        assert update.name == "New Name"
        assert update.entity_type is None


class TestMergeEntitiesRequestSchema:
    @pytest.mark.unit
    def test_valid_merge_request(self):
        req = MergeEntitiesRequest(source_id=1, target_id=2)
        assert req.source_id == 1
        assert req.target_id == 2

    @pytest.mark.unit
    def test_merge_request_requires_both_ids(self):
        with pytest.raises(ValidationError):
            MergeEntitiesRequest(source_id=1)


class TestRelationResponseSchema:
    @pytest.mark.unit
    def test_valid_relation_response(self):
        resp = RelationResponse(
            id=1,
            subject=EntityBrief(id=1, name="Edi", entity_type="person"),
            predicate="lives_in",
            object=EntityBrief(id=2, name="Berlin", entity_type="place"),
            confidence=0.9,
            created_at="2026-01-01T00:00:00",
        )
        assert resp.predicate == "lives_in"
        assert resp.subject.name == "Edi"
        assert resp.object.name == "Berlin"

    @pytest.mark.unit
    def test_relation_response_nullable_entities(self):
        resp = RelationResponse(id=1, predicate="unknown")
        assert resp.subject is None
        assert resp.object is None
        assert resp.confidence == 0.8


class TestKGStatsResponseSchema:
    @pytest.mark.unit
    def test_valid_stats(self):
        stats = KGStatsResponse(
            entity_count=100,
            relation_count=50,
            entity_types={"person": 40, "place": 30, "thing": 30},
        )
        assert stats.entity_count == 100
        assert stats.entity_types["person"] == 40

    @pytest.mark.unit
    def test_stats_defaults(self):
        stats = KGStatsResponse()
        assert stats.entity_count == 0
        assert stats.relation_count == 0
        assert stats.entity_types == {}


class TestEntityListResponseSchema:
    @pytest.mark.unit
    def test_empty_list(self):
        resp = EntityListResponse(entities=[], total=0)
        assert resp.entities == []
        assert resp.page == 1
        assert resp.size == 50

    @pytest.mark.unit
    def test_with_entities(self):
        resp = EntityListResponse(
            entities=[EntityResponse(id=1, name="X", entity_type="thing")],
            total=1,
            page=2,
            size=25,
        )
        assert len(resp.entities) == 1
        assert resp.page == 2
        assert resp.size == 25


class TestRelationListResponseSchema:
    @pytest.mark.unit
    def test_empty_list(self):
        resp = RelationListResponse(relations=[], total=0)
        assert resp.relations == []

    @pytest.mark.unit
    def test_with_relations(self):
        resp = RelationListResponse(
            relations=[RelationResponse(id=1, predicate="knows")],
            total=1,
        )
        assert len(resp.relations) == 1
