"""
Tests for Knowledge Graph Service — Entity resolution, relation saving,
extraction parsing, context retrieval formatting, and document extraction.

Uses in-memory SQLite (no pgvector). Embedding generation is mocked.
pgvector SQL queries are tested for error handling; actual similarity
search requires PostgreSQL and is covered by e2e tests.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import KG_ENTITY_TYPES, KGEntity, KGRelation
from services.knowledge_graph_service import (
    KnowledgeGraphService,
    kg_post_document_ingest_hook,
)

# ==========================================================================
# Fixtures
# ==========================================================================

@pytest.fixture
def kg_service(db_session: AsyncSession):
    """Create KnowledgeGraphService with test database.

    Embedding is mocked to return None (SQLite doesn't support pgvector).
    Tests that need embedding behavior mock _get_embedding specifically.
    """
    svc = KnowledgeGraphService(db_session)
    # Return None to avoid writing vector data to SQLite
    svc._get_embedding = AsyncMock(return_value=None)
    return svc


async def _create_entity(
    db_session: AsyncSession,
    name: str = "TestEntity",
    entity_type: str = "person",
    user_id: int | None = None,
    is_active: bool = True,
    mention_count: int = 1,
) -> KGEntity:
    entity = KGEntity(
        name=name,
        entity_type=entity_type,
        user_id=user_id,
        is_active=is_active,
        mention_count=mention_count,
    )
    db_session.add(entity)
    await db_session.commit()
    await db_session.refresh(entity)
    return entity


async def _create_relation(
    db_session: AsyncSession,
    subject_id: int,
    predicate: str,
    object_id: int,
    user_id: int | None = None,
    confidence: float = 0.8,
    is_active: bool = True,
) -> KGRelation:
    relation = KGRelation(
        subject_id=subject_id,
        predicate=predicate,
        object_id=object_id,
        user_id=user_id,
        confidence=confidence,
        is_active=is_active,
    )
    db_session.add(relation)
    await db_session.commit()
    await db_session.refresh(relation)
    return relation


# ==========================================================================
# Entity Resolution
# ==========================================================================

class TestResolveEntity:
    """Tests for KnowledgeGraphService.resolve_entity()."""

    @pytest.mark.unit
    async def test_resolve_entity_exact_match(self, kg_service, db_session):
        """Existing entity found by exact name match."""
        existing = await _create_entity(db_session, name="Edi", entity_type="person")

        result = await kg_service.resolve_entity("Edi", "person", user_id=None)

        assert result.id == existing.id
        assert result.mention_count == 2  # incremented

    @pytest.mark.unit
    async def test_resolve_entity_case_insensitive(self, kg_service, db_session):
        """Entity resolution is case-insensitive."""
        existing = await _create_entity(db_session, name="Berlin", entity_type="place")

        result = await kg_service.resolve_entity("berlin", "place", user_id=None)

        assert result.id == existing.id

    @pytest.mark.unit
    async def test_resolve_entity_creates_new(self, kg_service, db_session):
        """No match creates a new entity."""
        # Mock _find_similar_entity to return None (no embedding match)
        kg_service._find_similar_entity = AsyncMock(return_value=None)

        result = await kg_service.resolve_entity("NewEntity", "concept", user_id=None)

        assert result.name == "NewEntity"
        assert result.entity_type == "concept"
        assert result.mention_count == 1

    @pytest.mark.unit
    async def test_resolve_entity_with_description(self, kg_service, db_session):
        """Description is saved on new entity."""
        kg_service._find_similar_entity = AsyncMock(return_value=None)

        result = await kg_service.resolve_entity(
            "Acme", "organization", user_id=None, description="A tech company"
        )

        assert result.description == "A tech company"

    @pytest.mark.unit
    async def test_resolve_entity_fills_description(self, kg_service, db_session):
        """Description is filled on existing entity if missing."""
        existing = await _create_entity(db_session, name="Edi", entity_type="person")
        assert existing.description is None

        result = await kg_service.resolve_entity(
            "Edi", "person", user_id=None, description="A developer"
        )

        assert result.id == existing.id
        assert result.description == "A developer"

    @pytest.mark.unit
    async def test_resolve_entity_invalid_type_falls_back(self, kg_service, db_session):
        """Invalid entity type falls back to 'thing'."""
        kg_service._find_similar_entity = AsyncMock(return_value=None)

        result = await kg_service.resolve_entity("Something", "invalid_type", user_id=None)

        assert result.entity_type == "thing"

    @pytest.mark.unit
    async def test_resolve_entity_user_isolation(self, kg_service, db_session, test_user):
        """Entities are isolated per user_id."""
        existing = await _create_entity(db_session, name="Edi", entity_type="person", user_id=test_user.id)
        kg_service._find_similar_entity = AsyncMock(return_value=None)

        # Different user (None) should not match
        result = await kg_service.resolve_entity("Edi", "person", user_id=None)

        assert result.id != existing.id


# ==========================================================================
# Save Relation
# ==========================================================================

class TestSaveRelation:
    """Tests for KnowledgeGraphService.save_relation()."""

    @pytest.mark.unit
    async def test_save_relation_new(self, kg_service, db_session):
        """New relation is created."""
        e1 = await _create_entity(db_session, name="Edi", entity_type="person")
        e2 = await _create_entity(db_session, name="Berlin", entity_type="place")

        result = await kg_service.save_relation(e1.id, "lives_in", e2.id)

        assert result.subject_id == e1.id
        assert result.predicate == "lives_in"
        assert result.object_id == e2.id
        assert result.confidence == 0.8

    @pytest.mark.unit
    async def test_save_relation_dedup(self, kg_service, db_session):
        """Same triple updates confidence instead of creating duplicate."""
        e1 = await _create_entity(db_session, name="Edi", entity_type="person")
        e2 = await _create_entity(db_session, name="Berlin", entity_type="place")

        rel1 = await _create_relation(db_session, e1.id, "lives_in", e2.id, confidence=0.6)
        rel2 = await kg_service.save_relation(e1.id, "lives_in", e2.id, confidence=0.9)

        assert rel2.id == rel1.id
        assert rel2.confidence == 0.9  # max(0.6, 0.9)

    @pytest.mark.unit
    async def test_save_relation_different_predicate_is_new(self, kg_service, db_session):
        """Different predicate creates a new relation."""
        e1 = await _create_entity(db_session, name="Edi", entity_type="person")
        e2 = await _create_entity(db_session, name="Berlin", entity_type="place")

        rel1 = await _create_relation(db_session, e1.id, "lives_in", e2.id)
        rel2 = await kg_service.save_relation(e1.id, "works_in", e2.id)

        assert rel2.id != rel1.id


# ==========================================================================
# Extraction Parsing
# ==========================================================================

class TestParseExtraction:
    """Tests for _parse_extraction_response."""

    @pytest.mark.unit
    def test_parse_valid_json(self):
        raw = '{"entities": [{"name": "Edi", "type": "person"}], "relations": []}'
        result = KnowledgeGraphService._parse_extraction_response(raw)
        assert result is not None
        assert len(result["entities"]) == 1
        assert result["entities"][0]["name"] == "Edi"

    @pytest.mark.unit
    def test_parse_with_markdown_code_block(self):
        raw = '```json\n{"entities": [], "relations": []}\n```'
        result = KnowledgeGraphService._parse_extraction_response(raw)
        assert result is not None
        assert result["entities"] == []

    @pytest.mark.unit
    def test_parse_with_extra_text(self):
        raw = 'Here is the JSON:\n{"entities": [{"name": "X", "type": "thing"}], "relations": []}\nDone.'
        result = KnowledgeGraphService._parse_extraction_response(raw)
        assert result is not None
        assert len(result["entities"]) == 1

    @pytest.mark.unit
    def test_parse_empty_string(self):
        assert KnowledgeGraphService._parse_extraction_response("") is None

    @pytest.mark.unit
    def test_parse_invalid_json(self):
        assert KnowledgeGraphService._parse_extraction_response("not json") is None

    @pytest.mark.unit
    def test_parse_array_returns_none(self):
        """Array (not object) returns None."""
        assert KnowledgeGraphService._parse_extraction_response("[1, 2, 3]") is None


# ==========================================================================
# Extract and Save
# ==========================================================================

class TestExtractAndSave:
    """Tests for KnowledgeGraphService.extract_and_save()."""

    @pytest.mark.unit
    async def test_extract_and_save_parses_llm_output(self, kg_service, db_session):
        """Mock LLM output is parsed and entities + relations are saved."""
        llm_response = MagicMock()
        llm_response.message.content = '''{
            "entities": [
                {"name": "Edi", "type": "person", "description": "A developer"},
                {"name": "Berlin", "type": "place"}
            ],
            "relations": [
                {"subject": "Edi", "predicate": "lives_in", "object": "Berlin", "confidence": 0.9}
            ]
        }'''

        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=llm_response)
        mock_client.embeddings = AsyncMock(
            return_value=MagicMock(embedding=[0.1] * 768)
        )
        kg_service._ollama_client = mock_client
        kg_service._find_similar_entity = AsyncMock(return_value=None)

        entities, relations = await kg_service.extract_and_save(
            "Ich bin Edi und wohne in Berlin",
            "Okay, ich merke mir das.",
            user_id=None,
        )

        assert len(entities) == 2
        assert len(relations) == 1
        assert any(e.name == "Edi" for e in entities)
        assert any(e.name == "Berlin" for e in entities)
        assert relations[0].predicate == "lives_in"

    @pytest.mark.unit
    async def test_extract_empty_result(self, kg_service, db_session):
        """Empty LLM response returns empty lists."""
        llm_response = MagicMock()
        llm_response.message.content = '{"entities": [], "relations": []}'

        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=llm_response)
        kg_service._ollama_client = mock_client

        entities, relations = await kg_service.extract_and_save(
            "Schalte das Licht ein",
            "Licht eingeschaltet.",
        )

        assert entities == []
        assert relations == []

    @pytest.mark.unit
    async def test_extract_llm_failure_returns_empty(self, kg_service, db_session):
        """LLM call failure returns empty lists gracefully."""
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(side_effect=Exception("LLM down"))
        kg_service._ollama_client = mock_client

        entities, relations = await kg_service.extract_and_save(
            "Test message", "Test response",
        )

        assert entities == []
        assert relations == []


# ==========================================================================
# Context Retrieval
# ==========================================================================

class TestGetRelevantContext:
    """Tests for KnowledgeGraphService.get_relevant_context()."""

    @pytest.mark.unit
    async def test_get_relevant_context_returns_none_when_no_embedding(self, kg_service, db_session):
        """Returns None when embedding is None."""
        result = await kg_service.get_relevant_context("Wo wohnt Edi?", user_id=None)

        # _get_embedding returns None (mocked), so no pgvector query is attempted
        assert result is None

    @pytest.mark.unit
    async def test_get_relevant_context_embedding_failure(self, kg_service, db_session):
        """Returns None when embedding generation fails."""
        kg_service._get_embedding = AsyncMock(side_effect=Exception("No model"))

        result = await kg_service.get_relevant_context("Test", user_id=None)

        assert result is None


# ==========================================================================
# CRUD Operations
# ==========================================================================

class TestCRUD:
    """Tests for list, update, delete, merge, stats operations."""

    @pytest.mark.unit
    async def test_list_entities(self, kg_service, db_session):
        """List returns active entities."""
        await _create_entity(db_session, name="A", entity_type="person")
        await _create_entity(db_session, name="B", entity_type="place")
        await _create_entity(db_session, name="C", entity_type="person", is_active=False)

        entities, total = await kg_service.list_entities()

        assert total == 2
        assert len(entities) == 2
        names = {e.name for e in entities}
        assert "A" in names
        assert "B" in names
        assert "C" not in names

    @pytest.mark.unit
    async def test_list_entities_type_filter(self, kg_service, db_session):
        await _create_entity(db_session, name="A", entity_type="person")
        await _create_entity(db_session, name="B", entity_type="place")

        entities, total = await kg_service.list_entities(entity_type="person")

        assert total == 1
        assert entities[0].name == "A"

    @pytest.mark.unit
    async def test_list_entities_search(self, kg_service, db_session):
        await _create_entity(db_session, name="Berlin", entity_type="place")
        await _create_entity(db_session, name="Hamburg", entity_type="place")

        entities, total = await kg_service.list_entities(search="ber")

        assert total == 1
        assert entities[0].name == "Berlin"

    @pytest.mark.unit
    async def test_update_entity(self, kg_service, db_session):
        entity = await _create_entity(db_session, name="Old Name", entity_type="person")

        result = await kg_service.update_entity(entity.id, name="New Name")

        assert result is not None
        assert result.name == "New Name"

    @pytest.mark.unit
    async def test_update_entity_not_found(self, kg_service):
        result = await kg_service.update_entity(99999, name="X")
        assert result is None

    @pytest.mark.unit
    async def test_delete_entity(self, kg_service, db_session):
        entity = await _create_entity(db_session, name="ToDelete", entity_type="thing")
        e2 = await _create_entity(db_session, name="Other", entity_type="thing")
        await _create_relation(db_session, entity.id, "related_to", e2.id)

        success = await kg_service.delete_entity(entity.id)

        assert success is True

        # Entity should be inactive
        result = await db_session.execute(
            select(KGEntity).where(KGEntity.id == entity.id)
        )
        deleted = result.scalar_one()
        assert deleted.is_active is False

        # Relation should also be inactive
        result = await db_session.execute(
            select(KGRelation).where(KGRelation.subject_id == entity.id)
        )
        rel = result.scalar_one()
        assert rel.is_active is False

    @pytest.mark.unit
    async def test_delete_entity_not_found(self, kg_service):
        success = await kg_service.delete_entity(99999)
        assert success is False

    @pytest.mark.unit
    async def test_merge_entities(self, kg_service, db_session):
        source = await _create_entity(db_session, name="Ed", entity_type="person", mention_count=3)
        target = await _create_entity(db_session, name="Edi", entity_type="person", mention_count=5)
        e3 = await _create_entity(db_session, name="Berlin", entity_type="place")

        await _create_relation(db_session, source.id, "lives_in", e3.id)

        result = await kg_service.merge_entities(source.id, target.id)

        assert result is not None
        assert result.id == target.id
        assert result.mention_count == 8  # 3 + 5

        # Source should be inactive
        src_result = await db_session.execute(
            select(KGEntity).where(KGEntity.id == source.id)
        )
        assert src_result.scalar_one().is_active is False

    @pytest.mark.unit
    async def test_delete_relation(self, kg_service, db_session):
        e1 = await _create_entity(db_session, name="A")
        e2 = await _create_entity(db_session, name="B")
        rel = await _create_relation(db_session, e1.id, "knows", e2.id)

        success = await kg_service.delete_relation(rel.id)
        assert success is True

    @pytest.mark.unit
    async def test_delete_relation_not_found(self, kg_service):
        success = await kg_service.delete_relation(99999)
        assert success is False

    @pytest.mark.unit
    async def test_get_stats(self, kg_service, db_session):
        e1 = await _create_entity(db_session, name="A", entity_type="person")
        await _create_entity(db_session, name="B", entity_type="place")
        e3 = await _create_entity(db_session, name="C", entity_type="person")
        await _create_relation(db_session, e1.id, "knows", e3.id)

        stats = await kg_service.get_stats()

        assert stats["entity_count"] == 3
        assert stats["relation_count"] == 1
        assert stats["entity_types"]["person"] == 2
        assert stats["entity_types"]["place"] == 1

    @pytest.mark.unit
    async def test_list_relations(self, kg_service, db_session):
        e1 = await _create_entity(db_session, name="A", entity_type="person")
        e2 = await _create_entity(db_session, name="B", entity_type="place")
        await _create_relation(db_session, e1.id, "lives_in", e2.id)

        relations, total = await kg_service.list_relations()

        assert total == 1
        assert len(relations) == 1
        assert relations[0]["predicate"] == "lives_in"
        assert relations[0]["subject"]["name"] == "A"
        assert relations[0]["object"]["name"] == "B"

    @pytest.mark.unit
    async def test_list_relations_filter_by_entity(self, kg_service, db_session):
        e1 = await _create_entity(db_session, name="A", entity_type="person")
        e2 = await _create_entity(db_session, name="B", entity_type="place")
        e3 = await _create_entity(db_session, name="C", entity_type="thing")
        await _create_relation(db_session, e1.id, "lives_in", e2.id)
        await _create_relation(db_session, e1.id, "owns", e3.id)

        # Filter by entity B — should only return the lives_in relation
        relations, total = await kg_service.list_relations(entity_id=e2.id)

        assert total == 1
        assert relations[0]["predicate"] == "lives_in"


# ==========================================================================
# Update Relation
# ==========================================================================

class TestUpdateRelation:
    """Tests for KnowledgeGraphService.update_relation()."""

    @pytest.mark.unit
    async def test_update_relation_predicate(self, kg_service, db_session):
        """Update predicate of an existing relation."""
        e1 = await _create_entity(db_session, name="A", entity_type="person")
        e2 = await _create_entity(db_session, name="B", entity_type="place")
        rel = await _create_relation(db_session, e1.id, "lives_in", e2.id)

        result = await kg_service.update_relation(rel.id, predicate="works_in")

        assert result is not None
        assert result.predicate == "works_in"
        assert result.subject_id == e1.id
        assert result.object_id == e2.id

    @pytest.mark.unit
    async def test_update_relation_confidence(self, kg_service, db_session):
        """Update confidence of an existing relation."""
        e1 = await _create_entity(db_session, name="A", entity_type="person")
        e2 = await _create_entity(db_session, name="B", entity_type="place")
        rel = await _create_relation(db_session, e1.id, "knows", e2.id, confidence=0.5)

        result = await kg_service.update_relation(rel.id, confidence=0.95)

        assert result is not None
        assert result.confidence == 0.95

    @pytest.mark.unit
    async def test_update_relation_subject(self, kg_service, db_session):
        """Update subject of an existing relation."""
        e1 = await _create_entity(db_session, name="A", entity_type="person")
        e2 = await _create_entity(db_session, name="B", entity_type="place")
        e3 = await _create_entity(db_session, name="C", entity_type="person")
        rel = await _create_relation(db_session, e1.id, "lives_in", e2.id)

        result = await kg_service.update_relation(rel.id, subject_id=e3.id)

        assert result is not None
        assert result.subject_id == e3.id
        assert result.object_id == e2.id

    @pytest.mark.unit
    async def test_update_relation_object(self, kg_service, db_session):
        """Update object of an existing relation."""
        e1 = await _create_entity(db_session, name="A", entity_type="person")
        e2 = await _create_entity(db_session, name="B", entity_type="place")
        e3 = await _create_entity(db_session, name="C", entity_type="place")
        rel = await _create_relation(db_session, e1.id, "lives_in", e2.id)

        result = await kg_service.update_relation(rel.id, object_id=e3.id)

        assert result is not None
        assert result.object_id == e3.id

    @pytest.mark.unit
    async def test_update_relation_self_link_raises(self, kg_service, db_session):
        """Self-link (subject == object) raises ValueError."""
        e1 = await _create_entity(db_session, name="A", entity_type="person")
        e2 = await _create_entity(db_session, name="B", entity_type="place")
        rel = await _create_relation(db_session, e1.id, "lives_in", e2.id)

        with pytest.raises(ValueError, match="Subject and object must be different"):
            await kg_service.update_relation(rel.id, subject_id=e2.id)

    @pytest.mark.unit
    async def test_update_relation_nonexistent_entity_raises(self, kg_service, db_session):
        """Referencing a non-existent entity raises ValueError."""
        e1 = await _create_entity(db_session, name="A", entity_type="person")
        e2 = await _create_entity(db_session, name="B", entity_type="place")
        rel = await _create_relation(db_session, e1.id, "lives_in", e2.id)

        with pytest.raises(ValueError, match="not found"):
            await kg_service.update_relation(rel.id, object_id=99999)

    @pytest.mark.unit
    async def test_update_relation_not_found(self, kg_service):
        """Non-existent relation returns None."""
        result = await kg_service.update_relation(99999, predicate="x")
        assert result is None

    @pytest.mark.unit
    async def test_update_relation_inactive_returns_none(self, kg_service, db_session):
        """Inactive relation returns None."""
        e1 = await _create_entity(db_session, name="A", entity_type="person")
        e2 = await _create_entity(db_session, name="B", entity_type="place")
        rel = await _create_relation(db_session, e1.id, "lives_in", e2.id, is_active=False)

        result = await kg_service.update_relation(rel.id, predicate="x")
        assert result is None


# ==========================================================================
# Extract from Text (Document Chunks)
# ==========================================================================

class TestExtractFromText:
    """Tests for KnowledgeGraphService.extract_from_text()."""

    @pytest.mark.unit
    async def test_extract_from_text_parses_entities(self, kg_service, db_session):
        """Mock LLM output is parsed and entities + relations are saved from text."""
        llm_response = MagicMock()
        llm_response.message.content = '''{
            "entities": [
                {"name": "Munich", "type": "place", "description": "Capital of Bavaria"},
                {"name": "BMW", "type": "organization"}
            ],
            "relations": [
                {"subject": "BMW", "predicate": "headquartered_in", "object": "Munich", "confidence": 0.95}
            ]
        }'''

        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=llm_response)
        mock_client.embeddings = AsyncMock(
            return_value=MagicMock(embedding=[0.1] * 768)
        )
        kg_service._ollama_client = mock_client
        kg_service._find_similar_entity = AsyncMock(return_value=None)

        entities, relations = await kg_service.extract_from_text(
            "BMW has its headquarters in Munich, the capital of Bavaria.",
            user_id=None,
            source_ref="doc:42",
        )

        assert len(entities) == 2
        assert len(relations) == 1
        assert any(e.name == "Munich" for e in entities)
        assert any(e.name == "BMW" for e in entities)
        assert relations[0].predicate == "headquartered_in"

    @pytest.mark.unit
    async def test_extract_from_text_empty_result(self, kg_service, db_session):
        """Empty LLM response returns empty lists."""
        llm_response = MagicMock()
        llm_response.message.content = '{"entities": [], "relations": []}'

        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=llm_response)
        kg_service._ollama_client = mock_client

        entities, relations = await kg_service.extract_from_text(
            "This is a table of contents with page numbers.",
        )

        assert entities == []
        assert relations == []

    @pytest.mark.unit
    async def test_extract_from_text_llm_failure(self, kg_service, db_session):
        """LLM failure returns empty lists gracefully."""
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(side_effect=Exception("LLM down"))
        kg_service._ollama_client = mock_client

        entities, relations = await kg_service.extract_from_text("Some text")

        assert entities == []
        assert relations == []


# ==========================================================================
# Extract from Chunks
# ==========================================================================

class TestExtractFromChunks:
    """Tests for KnowledgeGraphService.extract_from_chunks()."""

    @pytest.mark.unit
    async def test_extract_from_chunks_iterates(self, kg_service, db_session):
        """Iterates over chunks and aggregates results."""
        entity_a = MagicMock(spec=KGEntity)
        entity_a.name = "A"
        entity_b = MagicMock(spec=KGEntity)
        entity_b.name = "B"
        relation = MagicMock(spec=KGRelation)

        kg_service.extract_from_text = AsyncMock(
            side_effect=[
                ([entity_a], []),
                ([entity_b], [relation]),
            ]
        )

        entities, relations = await kg_service.extract_from_chunks(
            ["Chunk one about A.", "Chunk two about B."],
            source_ref="doc:1",
        )

        assert len(entities) == 2
        assert len(relations) == 1
        assert kg_service.extract_from_text.call_count == 2

    @pytest.mark.unit
    async def test_extract_from_chunks_skips_empty(self, kg_service, db_session):
        """Empty or whitespace-only chunks are skipped."""
        kg_service.extract_from_text = AsyncMock(return_value=([], []))

        await kg_service.extract_from_chunks(
            ["", "  ", "valid text"],
            source_ref="doc:2",
        )

        assert kg_service.extract_from_text.call_count == 1

    @pytest.mark.unit
    async def test_extract_from_chunks_handles_failure(self, kg_service, db_session):
        """One chunk failure doesn't stop processing of remaining chunks."""
        entity = MagicMock(spec=KGEntity)
        entity.name = "Good"

        kg_service.extract_from_text = AsyncMock(
            side_effect=[
                Exception("LLM error"),
                ([entity], []),
            ]
        )

        entities, _relations = await kg_service.extract_from_chunks(
            ["bad chunk", "good chunk"],
            source_ref="doc:3",
        )

        assert len(entities) == 1
        assert kg_service.extract_from_text.call_count == 2


# ==========================================================================
# Document Ingest Hook
# ==========================================================================

class TestDocumentIngestHook:
    """Tests for kg_post_document_ingest_hook()."""

    @pytest.mark.unit
    async def test_hook_calls_extract_from_chunks(self, db_session):
        """Hook creates service and calls extract_from_chunks."""
        mock_svc = MagicMock(spec=KnowledgeGraphService)
        mock_svc.extract_from_chunks = AsyncMock(return_value=([], []))

        mock_session_cls = MagicMock()
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=db_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        # Patch the import target inside the hook function
        import services.knowledge_graph_service as kg_mod
        with (
            patch.object(kg_mod, "KnowledgeGraphService", return_value=mock_svc),
            patch.dict("sys.modules", {"services.database": MagicMock(AsyncSessionLocal=mock_session_cls)}),
        ):
            await kg_post_document_ingest_hook(
                chunks=["chunk1", "chunk2"],
                document_id=42,
                user_id=1,
            )

            mock_svc.extract_from_chunks.assert_awaited_once_with(
                ["chunk1", "chunk2"],
                user_id=1,
                source_ref="doc:42",
                lang="de",
            )

    @pytest.mark.unit
    async def test_hook_no_document_id(self, db_session):
        """Hook handles None document_id gracefully."""
        mock_svc = MagicMock(spec=KnowledgeGraphService)
        mock_svc.extract_from_chunks = AsyncMock(return_value=([], []))

        mock_session_cls = MagicMock()
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=db_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        import services.knowledge_graph_service as kg_mod
        with (
            patch.object(kg_mod, "KnowledgeGraphService", return_value=mock_svc),
            patch.dict("sys.modules", {"services.database": MagicMock(AsyncSessionLocal=mock_session_cls)}),
        ):
            await kg_post_document_ingest_hook(
                chunks=["text"],
                document_id=None,
                user_id=None,
            )

            mock_svc.extract_from_chunks.assert_awaited_once_with(
                ["text"],
                user_id=None,
                source_ref=None,
                lang="de",
            )

    @pytest.mark.unit
    async def test_hook_handles_exception(self):
        """Hook catches exceptions without raising."""
        # Make the local import raise an exception
        broken_mod = MagicMock()
        broken_mod.AsyncSessionLocal = MagicMock(side_effect=Exception("DB down"))

        with patch.dict("sys.modules", {"services.database": broken_mod}):
            # Should not raise
            await kg_post_document_ingest_hook(
                chunks=["text"],
                document_id=1,
            )


# ==========================================================================
# Entity Validation (_is_valid_entity)
# ==========================================================================

class TestEntityValidation:
    """Tests for KnowledgeGraphService._is_valid_entity()."""

    # --- Valid entities (should pass) ---

    @pytest.mark.unit
    def test_valid_person_name(self):
        assert KnowledgeGraphService._is_valid_entity("Eduard van den Bongard", "person")

    @pytest.mark.unit
    def test_valid_organization(self):
        assert KnowledgeGraphService._is_valid_entity("Shell Deutschland GmbH", "organization")

    @pytest.mark.unit
    def test_valid_place(self):
        assert KnowledgeGraphService._is_valid_entity("Berlin", "place")

    @pytest.mark.unit
    def test_valid_short_name(self):
        assert KnowledgeGraphService._is_valid_entity("Ed", "person")

    @pytest.mark.unit
    def test_valid_concept(self):
        assert KnowledgeGraphService._is_valid_entity("Maschinenbau", "concept")

    # --- Rejected: empty/too short/too long ---

    @pytest.mark.unit
    def test_reject_empty(self):
        assert not KnowledgeGraphService._is_valid_entity("", "person")

    @pytest.mark.unit
    def test_reject_single_char(self):
        assert not KnowledgeGraphService._is_valid_entity("A", "person")

    @pytest.mark.unit
    def test_reject_too_long(self):
        assert not KnowledgeGraphService._is_valid_entity("X" * 121, "person")

    # --- Rejected: OCR spaced-out characters ---

    @pytest.mark.unit
    def test_reject_spaced_chars(self):
        assert not KnowledgeGraphService._is_valid_entity("F R E S E N", "organization")

    @pytest.mark.unit
    def test_reject_spaced_date(self):
        assert not KnowledgeGraphService._is_valid_entity("0 8 . 0 6 . 2 0 2 2", "event")

    # --- Rejected: URLs ---

    @pytest.mark.unit
    def test_reject_url_www(self):
        assert not KnowledgeGraphService._is_valid_entity("www.shell.de/energieeffizienz", "organization")

    @pytest.mark.unit
    def test_reject_url_http(self):
        assert not KnowledgeGraphService._is_valid_entity("https://example.com", "thing")

    @pytest.mark.unit
    def test_reject_url_path(self):
        assert not KnowledgeGraphService._is_valid_entity("tk.de/datenschutz", "organization")

    # --- Rejected: email ---

    @pytest.mark.unit
    def test_reject_email(self):
        assert not KnowledgeGraphService._is_valid_entity("info@example.com", "person")

    # --- Rejected: pure digits/symbols ---

    @pytest.mark.unit
    def test_reject_pure_digits(self):
        assert not KnowledgeGraphService._is_valid_entity("12345678", "thing")

    @pytest.mark.unit
    def test_reject_symbols(self):
        assert not KnowledgeGraphService._is_valid_entity("---", "thing")

    # --- Rejected: high digit ratio ---

    @pytest.mark.unit
    def test_reject_high_digit_ratio(self):
        assert not KnowledgeGraphService._is_valid_entity("DE811127597", "organization")

    @pytest.mark.unit
    def test_reject_reference_code(self):
        assert not KnowledgeGraphService._is_valid_entity("Y25588501619C", "thing")

    # --- Rejected: dates ---

    @pytest.mark.unit
    def test_reject_date_dot(self):
        assert not KnowledgeGraphService._is_valid_entity("08.06.2022", "event")

    @pytest.mark.unit
    def test_reject_date_dash(self):
        assert not KnowledgeGraphService._is_valid_entity("2022-06-08", "event")

    # --- Rejected: phone numbers ---

    @pytest.mark.unit
    def test_reject_phone(self):
        assert not KnowledgeGraphService._is_valid_entity("+49 30 123456", "thing")

    @pytest.mark.unit
    def test_reject_phone_slash(self):
        assert not KnowledgeGraphService._is_valid_entity("030/1234567", "thing")

    # --- Rejected: IBAN ---

    @pytest.mark.unit
    def test_reject_iban(self):
        assert not KnowledgeGraphService._is_valid_entity("DE89 3704 0044 0532 0130 00", "thing")

    # --- Rejected: reference codes (uppercase+digits, no spaces) ---

    @pytest.mark.unit
    def test_reject_refcode(self):
        assert not KnowledgeGraphService._is_valid_entity("DEUTDEMM", "organization")

    # --- Rejected: generic roles (person type only) ---

    @pytest.mark.unit
    def test_reject_generic_role_kunde(self):
        assert not KnowledgeGraphService._is_valid_entity("Kunde", "person")

    @pytest.mark.unit
    def test_reject_generic_role_sachbearbeiter(self):
        assert not KnowledgeGraphService._is_valid_entity("Sachbearbeiter", "person")

    @pytest.mark.unit
    def test_reject_generic_role_vermittler(self):
        assert not KnowledgeGraphService._is_valid_entity("Vermittler", "person")

    @pytest.mark.unit
    def test_generic_role_allowed_as_concept(self):
        """Generic roles are valid when NOT person type."""
        assert KnowledgeGraphService._is_valid_entity("Kunde", "concept")

    @pytest.mark.unit
    def test_reject_numbered_role(self):
        assert not KnowledgeGraphService._is_valid_entity("Bediener 2", "person")

    @pytest.mark.unit
    def test_reject_numbered_sachbearbeiter(self):
        assert not KnowledgeGraphService._is_valid_entity("Sachbearbeiter 3", "person")

    @pytest.mark.unit
    def test_numbered_non_role_allowed(self):
        """Numbered names that aren't generic roles pass."""
        assert KnowledgeGraphService._is_valid_entity("Terminal 2", "place")

    # --- Rejected: German month dates ---

    @pytest.mark.unit
    def test_reject_german_month_date(self):
        assert not KnowledgeGraphService._is_valid_entity("März 2013", "event")

    @pytest.mark.unit
    def test_reject_month_with_day(self):
        assert not KnowledgeGraphService._is_valid_entity("01. Januar 2019", "event")

    @pytest.mark.unit
    def test_reject_month_alone(self):
        assert not KnowledgeGraphService._is_valid_entity("Dezember", "event")

    @pytest.mark.unit
    def test_month_in_proper_name_allowed(self):
        """Proper names containing month substrings pass."""
        assert KnowledgeGraphService._is_valid_entity("Oktoberfest", "event")

    # --- Rejected: currency ---

    @pytest.mark.unit
    def test_reject_currency_eur(self):
        assert not KnowledgeGraphService._is_valid_entity("100 EUR", "thing")

    @pytest.mark.unit
    def test_reject_currency_euro_symbol(self):
        assert not KnowledgeGraphService._is_valid_entity("0,04 € /Minute", "thing")

    @pytest.mark.unit
    def test_valid_org_euro_substring(self):
        """Org names containing 'Euro' as substring pass (no currency code match)."""
        assert KnowledgeGraphService._is_valid_entity("Eurotunnel", "organization")

    # --- Rejected: field separators ---

    @pytest.mark.unit
    def test_reject_asterisk_separators(self):
        assert not KnowledgeGraphService._is_valid_entity("DUEL*MS*OS*BUEN/HA*BI", "thing")

    @pytest.mark.unit
    def test_reject_pipe_separator(self):
        assert not KnowledgeGraphService._is_valid_entity("Abteilung|Finanzen", "organization")

    # --- Rejected: Nr. labels ---

    @pytest.mark.unit
    def test_reject_vertragsnr(self):
        assert not KnowledgeGraphService._is_valid_entity("Vertragsnr. J269385", "thing")

    @pytest.mark.unit
    def test_reject_kunden_nr(self):
        assert not KnowledgeGraphService._is_valid_entity("Kunden Nr 12345", "thing")

    # --- Rejected: field label suffixes (non-person only) ---

    @pytest.mark.unit
    def test_reject_field_label_suffix_zahlungsbedingungen(self):
        assert not KnowledgeGraphService._is_valid_entity("Zahlungsbedingungen", "concept")

    @pytest.mark.unit
    def test_reject_field_label_suffix_vertragsnummer(self):
        assert not KnowledgeGraphService._is_valid_entity("Vertragsnummer", "thing")

    @pytest.mark.unit
    def test_reject_field_label_suffix_mahngebuehren(self):
        assert not KnowledgeGraphService._is_valid_entity("Mahngebühren", "thing")

    @pytest.mark.unit
    def test_field_label_allowed_for_person(self):
        """Person type is exempt from field label suffix check."""
        assert KnowledgeGraphService._is_valid_entity("Frau Anschrift", "person")

    # --- Valid: named things pass ---

    @pytest.mark.unit
    def test_valid_named_thing_iphone(self):
        assert KnowledgeGraphService._is_valid_entity("iPhone 15", "thing")

    @pytest.mark.unit
    def test_valid_named_thing_tesla(self):
        assert KnowledgeGraphService._is_valid_entity("Tesla Model 3", "thing")


class TestEntityValidationInExtraction:
    """Test that _is_valid_entity is called during extraction."""

    @pytest.mark.unit
    async def test_extract_filters_invalid_entities(self, kg_service, db_session):
        """Invalid entities from LLM output are filtered out before resolve_entity."""
        llm_response = MagicMock()
        llm_response.message.content = '''{
            "entities": [
                {"name": "Eduard van den Bongard", "type": "person"},
                {"name": "F R E S E N", "type": "organization"},
                {"name": "DE811127597", "type": "thing"},
                {"name": "www.shell.de/effizienz", "type": "organization"},
                {"name": "Berlin", "type": "place"},
                {"name": "Kunde", "type": "person"}
            ],
            "relations": []
        }'''

        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=llm_response)
        mock_client.embeddings = AsyncMock(
            return_value=MagicMock(embedding=[0.1] * 768)
        )
        kg_service._ollama_client = mock_client
        kg_service._find_similar_entity = AsyncMock(return_value=None)

        entities, _relations = await kg_service.extract_and_save(
            "Test message", "Test response", user_id=None,
        )

        # Only "Eduard van den Bongard" and "Berlin" should pass
        assert len(entities) == 2
        names = {e.name for e in entities}
        assert "Eduard van den Bongard" in names
        assert "Berlin" in names
        assert "F R E S E N" not in names
        assert "DE811127597" not in names
        assert "Kunde" not in names

    @pytest.mark.unit
    async def test_extract_from_text_filters_invalid(self, kg_service, db_session):
        """Invalid entities are also filtered in extract_from_text."""
        llm_response = MagicMock()
        llm_response.message.content = '''{
            "entities": [
                {"name": "Shell Deutschland", "type": "organization"},
                {"name": "+49 30 123456", "type": "thing"},
                {"name": "08.06.2022", "type": "event"}
            ],
            "relations": []
        }'''

        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=llm_response)
        mock_client.embeddings = AsyncMock(
            return_value=MagicMock(embedding=[0.1] * 768)
        )
        kg_service._ollama_client = mock_client
        kg_service._find_similar_entity = AsyncMock(return_value=None)

        entities, _relations = await kg_service.extract_from_text(
            "Document text", user_id=None,
        )

        assert len(entities) == 1
        assert entities[0].name == "Shell Deutschland"


# ==========================================================================
# Entity Type Constants
# ==========================================================================

class TestEntityTypeConstants:
    @pytest.mark.unit
    def test_entity_types(self):
        assert "person" in KG_ENTITY_TYPES
        assert "place" in KG_ENTITY_TYPES
        assert "organization" in KG_ENTITY_TYPES
        assert "thing" in KG_ENTITY_TYPES
        assert "event" in KG_ENTITY_TYPES
        assert "concept" in KG_ENTITY_TYPES
        assert len(KG_ENTITY_TYPES) == 6
