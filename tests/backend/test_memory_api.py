"""
Tests for Memory API — Service extensions (update, get_count) and Pydantic schemas.

Uses in-memory SQLite (no pgvector). Pattern follows test_conversation_memory.py.
"""
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.memory_schemas import (
    MemoryCreateRequest,
    MemoryListResponse,
    MemoryResponse,
    MemoryUpdateRequest,
)
from models.database import ConversationMemory
from services.conversation_memory_service import ConversationMemoryService

# ==========================================================================
# Fixtures
# ==========================================================================

@pytest.fixture
def memory_service(db_session: AsyncSession):
    """Create ConversationMemoryService with test database."""
    return ConversationMemoryService(db_session)


async def _create_memory(
    db_session: AsyncSession,
    content: str = "Test memory",
    category: str = "fact",
    user_id: int | None = None,
    importance: float = 0.5,
    is_active: bool = True,
) -> ConversationMemory:
    """Helper to create a memory in the test database."""
    memory = ConversationMemory(
        content=content,
        category=category,
        user_id=user_id,
        importance=importance,
        is_active=is_active,
    )
    db_session.add(memory)
    await db_session.commit()
    await db_session.refresh(memory)
    return memory


# ==========================================================================
# Service: update()
# ==========================================================================

class TestMemoryServiceUpdate:
    """Tests for ConversationMemoryService.update()."""

    @pytest.mark.unit
    async def test_update_content(self, memory_service, db_session):
        """Content is updated when provided."""
        memory = await _create_memory(db_session, content="Old content")

        result = await memory_service.update(memory.id, content="New content")

        assert result is not None
        assert result.content == "New content"
        assert result.category == "fact"  # unchanged

    @pytest.mark.unit
    async def test_update_category(self, memory_service, db_session):
        """Category is updated when provided."""
        memory = await _create_memory(db_session, category="fact")

        result = await memory_service.update(memory.id, category="preference")

        assert result is not None
        assert result.category == "preference"
        assert result.content == "Test memory"  # unchanged

    @pytest.mark.unit
    async def test_update_importance(self, memory_service, db_session):
        """Importance is updated when provided."""
        memory = await _create_memory(db_session, importance=0.5)

        result = await memory_service.update(memory.id, importance=0.9)

        assert result is not None
        assert result.importance == 0.9

    @pytest.mark.unit
    async def test_update_not_found(self, memory_service):
        """Non-existent memory ID returns None."""
        result = await memory_service.update(99999, content="Nope")
        assert result is None

    @pytest.mark.unit
    async def test_update_partial(self, memory_service, db_session):
        """Only provided fields are changed, others remain unchanged."""
        memory = await _create_memory(
            db_session, content="Original", category="fact", importance=0.3
        )

        result = await memory_service.update(memory.id, importance=0.8)

        assert result is not None
        assert result.content == "Original"
        assert result.category == "fact"
        assert result.importance == 0.8

    @pytest.mark.unit
    async def test_update_invalid_category(self, memory_service, db_session):
        """Invalid category returns None."""
        memory = await _create_memory(db_session)

        result = await memory_service.update(memory.id, category="invalid_cat")
        assert result is None

    @pytest.mark.unit
    async def test_update_inactive_memory(self, memory_service, db_session):
        """Inactive (deleted) memory cannot be updated."""
        memory = await _create_memory(db_session, is_active=False)

        result = await memory_service.update(memory.id, content="Updated")
        assert result is None


# ==========================================================================
# Service: get_count()
# ==========================================================================

class TestMemoryServiceGetCount:
    """Tests for ConversationMemoryService.get_count()."""

    @pytest.mark.unit
    async def test_get_count_basic(self, memory_service, db_session):
        """Counts active memories."""
        await _create_memory(db_session, content="One")
        await _create_memory(db_session, content="Two")

        count = await memory_service.get_count()
        assert count == 2

    @pytest.mark.unit
    async def test_get_count_with_category(self, memory_service, db_session):
        """Filters by category."""
        await _create_memory(db_session, category="fact")
        await _create_memory(db_session, category="preference")
        await _create_memory(db_session, category="fact")

        count = await memory_service.get_count(category="fact")
        assert count == 2

    @pytest.mark.unit
    async def test_get_count_excludes_inactive(self, memory_service, db_session):
        """Inactive memories are not counted."""
        await _create_memory(db_session, is_active=True)
        await _create_memory(db_session, is_active=False)

        count = await memory_service.get_count()
        assert count == 1

    @pytest.mark.unit
    async def test_get_count_with_user_id(self, memory_service, db_session, test_user):
        """Filters by user ID."""
        await _create_memory(db_session, user_id=test_user.id)
        await _create_memory(db_session, user_id=test_user.id)
        await _create_memory(db_session, user_id=None)

        count = await memory_service.get_count(user_id=test_user.id)
        assert count == 2

    @pytest.mark.unit
    async def test_get_count_empty(self, memory_service):
        """Returns 0 when no memories exist."""
        count = await memory_service.get_count()
        assert count == 0


# ==========================================================================
# Pydantic Schemas
# ==========================================================================

class TestMemorySchemas:
    """Tests for Pydantic request/response schemas."""

    @pytest.mark.unit
    def test_create_schema_valid(self):
        """Valid create request is accepted."""
        req = MemoryCreateRequest(
            content="User likes jazz music",
            category="preference",
            importance=0.8,
        )
        assert req.content == "User likes jazz music"
        assert req.category == "preference"
        assert req.importance == 0.8

    @pytest.mark.unit
    def test_create_schema_defaults(self):
        """Default importance is 0.5."""
        req = MemoryCreateRequest(content="Test", category="fact")
        assert req.importance == 0.5

    @pytest.mark.unit
    def test_create_schema_invalid_category(self):
        """Invalid category raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            MemoryCreateRequest(content="Test", category="invalid")
        assert "category" in str(exc_info.value)

    @pytest.mark.unit
    def test_create_schema_content_too_long(self):
        """Content exceeding 2000 chars raises ValidationError."""
        with pytest.raises(ValidationError):
            MemoryCreateRequest(content="x" * 2001, category="fact")

    @pytest.mark.unit
    def test_create_schema_content_empty(self):
        """Empty content raises ValidationError."""
        with pytest.raises(ValidationError):
            MemoryCreateRequest(content="", category="fact")

    @pytest.mark.unit
    def test_create_schema_importance_out_of_range(self):
        """Importance outside 0.1-1.0 raises ValidationError."""
        with pytest.raises(ValidationError):
            MemoryCreateRequest(content="Test", category="fact", importance=0.0)
        with pytest.raises(ValidationError):
            MemoryCreateRequest(content="Test", category="fact", importance=1.5)

    @pytest.mark.unit
    def test_update_schema_partial(self):
        """Partial update request with only some fields."""
        req = MemoryUpdateRequest(importance=0.9)
        assert req.content is None
        assert req.category is None
        assert req.importance == 0.9

    @pytest.mark.unit
    def test_update_schema_empty(self):
        """All-None update request is valid."""
        req = MemoryUpdateRequest()
        assert req.content is None
        assert req.category is None
        assert req.importance is None

    @pytest.mark.unit
    def test_update_schema_invalid_category(self):
        """Invalid category in update raises ValidationError."""
        with pytest.raises(ValidationError):
            MemoryUpdateRequest(category="nonexistent")

    @pytest.mark.unit
    def test_response_schema(self):
        """Response schema accepts expected fields."""
        resp = MemoryResponse(
            id=1,
            content="Test",
            category="fact",
            importance=0.5,
            access_count=3,
            created_at="2024-01-01T00:00:00",
            last_accessed_at=None,
        )
        assert resp.id == 1
        assert resp.access_count == 3
        assert resp.last_accessed_at is None

    @pytest.mark.unit
    def test_list_response_schema(self):
        """List response schema with pagination."""
        resp = MemoryListResponse(
            memories=[
                MemoryResponse(
                    id=1,
                    content="Test",
                    category="fact",
                    importance=0.5,
                    access_count=0,
                    created_at="2024-01-01T00:00:00",
                    last_accessed_at=None,
                )
            ],
            total=1,
            limit=50,
            offset=0,
        )
        assert len(resp.memories) == 1
        assert resp.total == 1
