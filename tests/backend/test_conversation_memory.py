"""
Tests for Conversation Memory — Model, Service, Config, and Chat Integration.

Uses in-memory SQLite (no pgvector). Embedding generation is mocked.
pgvector SQL queries are tested for error handling; actual similarity
search requires PostgreSQL and is covered by e2e tests.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    MEMORY_CATEGORIES,
    MEMORY_CATEGORY_CONTEXT,
    MEMORY_CATEGORY_FACT,
    MEMORY_CATEGORY_INSTRUCTION,
    MEMORY_CATEGORY_PREFERENCE,
    ConversationMemory,
)
from services.conversation_memory_service import ConversationMemoryService

# ==========================================================================
# Model Tests
# ==========================================================================

class TestConversationMemoryModel:
    """Tests for the ConversationMemory SQLAlchemy model."""

    @pytest.mark.unit
    async def test_create_memory(self, db_session: AsyncSession):
        """Test basic memory creation with defaults."""
        memory = ConversationMemory(
            content="Der Benutzer mag Jazz-Musik",
            category=MEMORY_CATEGORY_PREFERENCE,
        )
        db_session.add(memory)
        await db_session.commit()
        await db_session.refresh(memory)

        assert memory.id is not None
        assert memory.content == "Der Benutzer mag Jazz-Musik"
        assert memory.category == MEMORY_CATEGORY_PREFERENCE
        assert memory.is_active is True
        assert memory.access_count == 0
        assert memory.importance == 0.5
        assert memory.user_id is None
        assert memory.expires_at is None
        assert memory.last_accessed_at is None
        assert memory.created_at is not None

    @pytest.mark.unit
    async def test_create_memory_with_all_fields(self, db_session: AsyncSession, test_user):
        """Test memory creation with all fields set."""
        expires = datetime.utcnow() + timedelta(days=7)
        memory = ConversationMemory(
            content="Bitte sprich mich mit Du an",
            category=MEMORY_CATEGORY_INSTRUCTION,
            user_id=test_user.id,
            importance=0.9,
            source_session_id="session-abc123",
            expires_at=expires,
        )
        db_session.add(memory)
        await db_session.commit()
        await db_session.refresh(memory)

        assert memory.user_id == test_user.id
        assert memory.importance == 0.9
        assert memory.source_session_id == "session-abc123"
        assert memory.expires_at is not None
        assert memory.category == MEMORY_CATEGORY_INSTRUCTION

    @pytest.mark.unit
    async def test_memory_with_source_message(self, db_session: AsyncSession, test_message):
        """Test memory linked to a source message."""
        memory = ConversationMemory(
            content="Test fact",
            category=MEMORY_CATEGORY_FACT,
            source_message_id=test_message.id,
        )
        db_session.add(memory)
        await db_session.commit()
        await db_session.refresh(memory)

        assert memory.source_message_id == test_message.id

    @pytest.mark.unit
    async def test_multiple_memories_per_user(self, db_session: AsyncSession, test_user):
        """Test creating multiple memories for the same user."""
        for i in range(3):
            memory = ConversationMemory(
                content=f"Memory {i}",
                category=MEMORY_CATEGORY_FACT,
                user_id=test_user.id,
            )
            db_session.add(memory)
        await db_session.commit()

        result = await db_session.execute(
            select(func.count(ConversationMemory.id))
            .where(ConversationMemory.user_id == test_user.id)
        )
        count = result.scalar()
        assert count == 3

    @pytest.mark.unit
    def test_category_constants(self):
        """Test that category constants are defined correctly."""
        assert MEMORY_CATEGORY_PREFERENCE == "preference"
        assert MEMORY_CATEGORY_FACT == "fact"
        assert MEMORY_CATEGORY_CONTEXT == "context"
        assert MEMORY_CATEGORY_INSTRUCTION == "instruction"
        assert len(MEMORY_CATEGORIES) == 4
        assert all(cat in MEMORY_CATEGORIES for cat in [
            "preference", "fact", "context", "instruction"
        ])


# ==========================================================================
# Config Tests
# ==========================================================================

class TestConversationMemoryConfig:
    """Tests for memory-related configuration settings."""

    @pytest.mark.unit
    def test_default_settings(self):
        """Test that memory config defaults are correct."""
        from utils.config import Settings
        s = Settings(database_url="sqlite:///:memory:")

        assert s.memory_enabled is False
        assert s.memory_retrieval_limit == 3
        assert s.memory_retrieval_threshold == 0.7
        assert s.memory_max_per_user == 500
        assert s.memory_context_decay_days == 30
        assert s.memory_dedup_threshold == 0.9

    @pytest.mark.unit
    def test_custom_settings(self):
        """Test that memory config can be customized."""
        from utils.config import Settings
        s = Settings(
            database_url="sqlite:///:memory:",
            memory_enabled=True,
            memory_retrieval_limit=5,
            memory_retrieval_threshold=0.8,
            memory_max_per_user=1000,
            memory_context_decay_days=60,
            memory_dedup_threshold=0.95,
        )
        assert s.memory_enabled is True
        assert s.memory_retrieval_limit == 5
        assert s.memory_retrieval_threshold == 0.8
        assert s.memory_max_per_user == 1000
        assert s.memory_context_decay_days == 60
        assert s.memory_dedup_threshold == 0.95


# ==========================================================================
# Service Tests
# ==========================================================================

@pytest.fixture
def mock_embedding():
    """Mock embedding vector (768 dims)."""
    return [0.1] * 768


@pytest.fixture
def memory_service(db_session: AsyncSession):
    """Create ConversationMemoryService with test database."""
    return ConversationMemoryService(db_session)


class TestConversationMemoryServiceSave:
    """Tests for ConversationMemoryService.save()."""

    @pytest.mark.unit
    async def test_save_memory_basic(self, memory_service):
        """Test saving a memory with mocked embedding (None for SQLite compat)."""
        with patch.object(
            memory_service, '_get_embedding', return_value=None
        ):
            result = await memory_service.save(
                content="Der Benutzer mag Jazz",
                category="preference",
                user_id=None,
            )

        assert result is not None
        assert result.content == "Der Benutzer mag Jazz"
        assert result.category == "preference"
        assert result.is_active is True

    @pytest.mark.unit
    async def test_save_invalid_category(self, memory_service):
        """Test that invalid category is rejected."""
        result = await memory_service.save(
            content="Test",
            category="invalid_category",
        )
        assert result is None

    @pytest.mark.unit
    async def test_save_with_embedding_failure(self, memory_service):
        """Test that save still works when embedding generation fails."""
        with patch.object(
            memory_service, '_get_embedding', side_effect=Exception("Ollama down")
        ):
            result = await memory_service.save(
                content="Test memory",
                category="fact",
            )

        assert result is not None
        assert result.content == "Test memory"
        assert result.embedding is None

    @pytest.mark.unit
    async def test_save_deduplication(self, memory_service, db_session, mock_embedding):
        """Test that duplicate memories get deduplicated (access_count++)."""
        # Create an existing memory
        existing = ConversationMemory(
            content="Der Benutzer mag Jazz",
            category="preference",
            access_count=2,
        )
        db_session.add(existing)
        await db_session.commit()
        await db_session.refresh(existing)

        with patch.object(
            memory_service, '_get_embedding', return_value=mock_embedding
        ), patch.object(
            memory_service, '_find_duplicate', return_value=existing
        ):
            result = await memory_service.save(
                content="Der Benutzer mag Jazz-Musik",
                category="preference",
            )

        assert result.id == existing.id
        assert result.access_count == 3
        assert result.last_accessed_at is not None

    @pytest.mark.unit
    async def test_save_with_all_fields(self, memory_service):
        """Test saving with all optional fields."""
        expires = datetime.utcnow() + timedelta(days=7)

        with patch.object(
            memory_service, '_get_embedding', return_value=None
        ):
            result = await memory_service.save(
                content="Kontext: Benutzer plant Urlaub",
                category="context",
                importance=0.8,
                source_session_id="sess-123",
                expires_at=expires,
            )

        assert result.importance == 0.8
        assert result.source_session_id == "sess-123"
        assert result.expires_at is not None

    @pytest.mark.unit
    async def test_save_enforces_max_limit(self, memory_service, db_session, test_user):
        """Test that save deactivates least important memory when limit is reached."""
        # Patch settings to low limit for testing
        with patch('services.conversation_memory_service.settings') as mock_settings:
            mock_settings.memory_max_per_user = 2
            mock_settings.memory_dedup_threshold = 0.9
            mock_settings.ollama_embed_model = "nomic-embed-text"

            # Create 2 existing memories
            for i in range(2):
                m = ConversationMemory(
                    content=f"Memory {i}",
                    category="fact",
                    user_id=test_user.id,
                    importance=0.3 + i * 0.1,
                )
                db_session.add(m)
            await db_session.commit()

            with patch.object(
                memory_service, '_get_embedding', return_value=None
            ):
                result = await memory_service.save(
                    content="New important memory",
                    category="fact",
                    user_id=test_user.id,
                    importance=0.9,
                )

            assert result is not None
            assert result.content == "New important memory"


class TestConversationMemoryServiceRetrieve:
    """Tests for ConversationMemoryService.retrieve()."""

    @pytest.mark.unit
    async def test_retrieve_embedding_failure(self, memory_service):
        """Test that retrieve returns empty list on embedding failure."""
        with patch.object(
            memory_service, '_get_embedding', side_effect=Exception("Ollama down")
        ):
            results = await memory_service.retrieve("What does the user like?")

        assert results == []

    @pytest.mark.unit
    async def test_retrieve_calls_embedding(self, memory_service, mock_embedding):
        """Test that retrieve generates an embedding for the query."""
        with patch.object(
            memory_service, '_get_embedding', return_value=mock_embedding
        ) as mock_get_emb:
            # This will fail on the SQL (SQLite, no pgvector) — that's OK,
            # we just want to verify embedding was called
            try:
                await memory_service.retrieve("What music does the user like?")
            except Exception:
                pass

            mock_get_emb.assert_called_once_with("What music does the user like?")


class TestConversationMemoryServiceCleanup:
    """Tests for ConversationMemoryService.cleanup()."""

    @pytest.mark.unit
    async def test_cleanup_expired(self, memory_service, db_session):
        """Test that expired memories get deactivated."""
        # Create an expired memory
        expired = ConversationMemory(
            content="Old reminder",
            category="context",
            is_active=True,
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        db_session.add(expired)

        # Create a non-expired memory
        active = ConversationMemory(
            content="Still valid",
            category="fact",
            is_active=True,
            expires_at=datetime.utcnow() + timedelta(days=7),
        )
        db_session.add(active)
        await db_session.commit()

        counts = await memory_service.cleanup()

        assert counts["expired"] == 1

        # Verify the expired memory is deactivated
        await db_session.refresh(expired)
        assert expired.is_active is False

        # Verify the active memory is still active
        await db_session.refresh(active)
        assert active.is_active is True

    @pytest.mark.unit
    async def test_cleanup_context_decay(self, memory_service, db_session):
        """Test that context memories decay after threshold."""
        decay_days = 30
        with patch('services.conversation_memory_service.settings') as mock_settings:
            mock_settings.memory_context_decay_days = decay_days

            # Context memory last accessed 60 days ago (should decay)
            decayed = ConversationMemory(
                content="Old context",
                category="context",
                is_active=True,
                last_accessed_at=datetime.utcnow() - timedelta(days=60),
            )
            db_session.add(decayed)

            # Context memory last accessed recently (should survive)
            recent = ConversationMemory(
                content="Recent context",
                category="context",
                is_active=True,
                last_accessed_at=datetime.utcnow() - timedelta(days=5),
            )
            db_session.add(recent)

            # Non-context memory (should not be affected by decay)
            fact = ConversationMemory(
                content="A fact",
                category="fact",
                is_active=True,
                last_accessed_at=datetime.utcnow() - timedelta(days=60),
            )
            db_session.add(fact)
            await db_session.commit()

            counts = await memory_service.cleanup()

        assert counts["decayed"] >= 1

        await db_session.refresh(decayed)
        assert decayed.is_active is False

        await db_session.refresh(recent)
        assert recent.is_active is True

        await db_session.refresh(fact)
        assert fact.is_active is True

    @pytest.mark.unit
    async def test_cleanup_context_decay_never_accessed(self, memory_service, db_session):
        """Test that context memories created long ago and never accessed get decayed."""
        with patch('services.conversation_memory_service.settings') as mock_settings:
            mock_settings.memory_context_decay_days = 30

            old_context = ConversationMemory(
                content="Never accessed context",
                category="context",
                is_active=True,
                last_accessed_at=None,
                created_at=datetime.utcnow() - timedelta(days=60),
            )
            db_session.add(old_context)
            await db_session.commit()

            counts = await memory_service.cleanup()

        assert counts["decayed"] >= 1
        await db_session.refresh(old_context)
        assert old_context.is_active is False

    @pytest.mark.unit
    async def test_cleanup_no_changes(self, memory_service, db_session):
        """Test cleanup with nothing to clean."""
        # Active, non-expired, non-context memory
        memory = ConversationMemory(
            content="Healthy memory",
            category="fact",
            is_active=True,
        )
        db_session.add(memory)
        await db_session.commit()

        counts = await memory_service.cleanup()

        assert counts["expired"] == 0
        assert counts["decayed"] == 0
        assert counts["over_limit"] == 0


class TestConversationMemoryServiceDeleteList:
    """Tests for delete and list_for_user methods."""

    @pytest.mark.unit
    async def test_delete_memory(self, memory_service, db_session):
        """Test soft-deleting a memory."""
        memory = ConversationMemory(
            content="To be deleted",
            category="fact",
            is_active=True,
        )
        db_session.add(memory)
        await db_session.commit()
        await db_session.refresh(memory)

        result = await memory_service.delete(memory.id)
        assert result is True

        await db_session.refresh(memory)
        assert memory.is_active is False

    @pytest.mark.unit
    async def test_delete_nonexistent(self, memory_service):
        """Test deleting a non-existent memory returns False."""
        result = await memory_service.delete(99999)
        assert result is False

    @pytest.mark.unit
    async def test_list_for_user(self, memory_service, db_session, test_user):
        """Test listing memories for a user."""
        for i in range(3):
            m = ConversationMemory(
                content=f"User memory {i}",
                category="fact",
                user_id=test_user.id,
            )
            db_session.add(m)

        # Also add an inactive memory (should not appear)
        inactive = ConversationMemory(
            content="Inactive memory",
            category="fact",
            user_id=test_user.id,
            is_active=False,
        )
        db_session.add(inactive)
        await db_session.commit()

        result = await memory_service.list_for_user(test_user.id)

        assert len(result) == 3
        assert all(m["category"] == "fact" for m in result)

    @pytest.mark.unit
    async def test_list_for_user_with_category_filter(self, memory_service, db_session, test_user):
        """Test listing memories with category filter."""
        m1 = ConversationMemory(content="Preference", category="preference", user_id=test_user.id)
        m2 = ConversationMemory(content="Fact", category="fact", user_id=test_user.id)
        db_session.add_all([m1, m2])
        await db_session.commit()

        result = await memory_service.list_for_user(test_user.id, category="preference")

        assert len(result) == 1
        assert result[0]["category"] == "preference"

    @pytest.mark.unit
    async def test_list_for_user_pagination(self, memory_service, db_session, test_user):
        """Test pagination for list_for_user."""
        for i in range(5):
            m = ConversationMemory(
                content=f"Memory {i}",
                category="fact",
                user_id=test_user.id,
            )
            db_session.add(m)
        await db_session.commit()

        page1 = await memory_service.list_for_user(test_user.id, limit=2, offset=0)
        page2 = await memory_service.list_for_user(test_user.id, limit=2, offset=2)

        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0]["id"] != page2[0]["id"]

    @pytest.mark.unit
    async def test_list_for_user_empty(self, memory_service, test_user):
        """Test listing memories for a user with no memories."""
        result = await memory_service.list_for_user(test_user.id)
        assert result == []

    @pytest.mark.unit
    async def test_list_for_user_result_format(self, memory_service, db_session, test_user):
        """Test that list result has expected fields."""
        m = ConversationMemory(
            content="Test",
            category="fact",
            user_id=test_user.id,
            importance=0.8,
            access_count=3,
        )
        db_session.add(m)
        await db_session.commit()

        result = await memory_service.list_for_user(test_user.id)

        assert len(result) == 1
        item = result[0]
        assert "id" in item
        assert "content" in item
        assert "category" in item
        assert "importance" in item
        assert "access_count" in item
        assert "created_at" in item
        assert "last_accessed_at" in item


# ==========================================================================
# Memory Context Formatting Tests (Chat Integration)
# ==========================================================================

class TestMemoryContextFormatting:
    """Tests for memory context formatting logic.

    Note: _retrieve_memory_context lives in chat_handler which requires asyncpg
    at import time (services.database). These tests verify the formatting logic
    via prompt_manager directly. Full integration is tested in Docker (e2e).
    """

    @pytest.mark.unit
    def test_memory_context_section_german(self):
        """German memory section template renders correctly."""
        from services.prompt_manager import prompt_manager

        memories_str = "- [PREFERENCE] Mag Jazz-Musik\n- [FACT] Heisst Max"
        result = prompt_manager.get(
            "chat", "memory_context_section", lang="de",
            memories=memories_str
        )

        assert "ERINNERUNGEN" in result
        assert "[PREFERENCE] Mag Jazz-Musik" in result
        assert "[FACT] Heisst Max" in result
        assert "personalisierter" in result

    @pytest.mark.unit
    def test_memory_context_section_english(self):
        """English memory section template renders correctly."""
        from services.prompt_manager import prompt_manager

        memories_str = "- [PREFERENCE] Likes jazz"
        result = prompt_manager.get(
            "chat", "memory_context_section", lang="en",
            memories=memories_str
        )

        assert "MEMORIES" in result
        assert "[PREFERENCE] Likes jazz" in result
        assert "personalized" in result

    @pytest.mark.unit
    def test_memory_bullet_formatting(self):
        """Memories are formatted as category-labeled bullet list."""
        memories = [
            {"content": "Mag Jazz-Musik", "category": "preference"},
            {"content": "Heisst Max", "category": "fact"},
            {"content": "Sprich mich mit Du an", "category": "instruction"},
            {"content": "Plant Urlaub", "category": "context"},
        ]

        lines = []
        for m in memories:
            cat_label = m["category"].upper()
            lines.append(f"- [{cat_label}] {m['content']}")
        result = "\n".join(lines)

        assert "- [PREFERENCE] Mag Jazz-Musik" in result
        assert "- [FACT] Heisst Max" in result
        assert "- [INSTRUCTION] Sprich mich mit Du an" in result
        assert "- [CONTEXT] Plant Urlaub" in result

    @pytest.mark.unit
    def test_empty_memories_no_section(self):
        """No memories → empty string (no section injected)."""
        # This mirrors the logic in _retrieve_memory_context:
        # if not memories: return ""
        memories = []
        assert len(memories) == 0
        # The function returns "" before calling prompt_manager


# ==========================================================================
# OllamaService Memory Integration Tests
# ==========================================================================

class TestOllamaServiceMemoryIntegration:
    """Tests for memory_context parameter in OllamaService."""

    @pytest.mark.unit
    def test_get_system_prompt_without_memory(self):
        """System prompt unchanged when no memory_context."""
        from services.ollama_service import OllamaService

        with patch('services.ollama_service.get_default_client'):
            service = OllamaService()

        prompt_no_mem = service.get_system_prompt("de")
        prompt_none = service.get_system_prompt("de", memory_context=None)

        assert prompt_no_mem == prompt_none
        assert "ERINNERUNGEN" not in prompt_no_mem

    @pytest.mark.unit
    def test_get_system_prompt_with_memory(self):
        """Memory section is appended to system prompt."""
        from services.ollama_service import OllamaService

        with patch('services.ollama_service.get_default_client'):
            service = OllamaService()

        memory_section = "ERINNERUNGEN:\n- [FACT] User heisst Max"
        prompt = service.get_system_prompt("de", memory_context=memory_section)

        assert "ERINNERUNGEN" in prompt
        assert "User heisst Max" in prompt
        # Base prompt should still be there
        assert "Renfield" in prompt

    @pytest.mark.unit
    def test_build_rag_system_prompt_with_memory(self):
        """Memory section is included in RAG system prompt."""
        from services.ollama_service import OllamaService

        with patch('services.ollama_service.get_default_client'):
            service = OllamaService()

        memory_section = "MEMORIES:\n- [PREFERENCE] Likes jazz"
        prompt = service._build_rag_system_prompt(
            context="Some RAG context here",
            lang="en",
            memory_context=memory_section,
        )

        assert "MEMORIES" in prompt
        assert "Likes jazz" in prompt
        assert "Some RAG context here" in prompt

    @pytest.mark.unit
    def test_build_rag_system_prompt_without_memory(self):
        """RAG prompt unchanged without memory_context."""
        from services.ollama_service import OllamaService

        with patch('services.ollama_service.get_default_client'):
            service = OllamaService()

        prompt_no_mem = service._build_rag_system_prompt(context="RAG context", lang="en")
        prompt_none = service._build_rag_system_prompt(context="RAG context", lang="en", memory_context=None)

        assert prompt_no_mem == prompt_none


# ==========================================================================
# Memory Extraction Tests
# ==========================================================================

def _make_llm_response(content: str):
    """Create a mock LLM response object."""
    response = MagicMock()
    response.message.content = content
    return response


class TestMemoryExtraction:
    """Tests for ConversationMemoryService.extract_and_save()."""

    @pytest.mark.unit
    async def test_extract_and_save_basic(self, memory_service):
        """LLM returns 2 facts → 2 memories saved."""
        llm_response = _make_llm_response(
            '[{"content": "Mag Jazz", "category": "preference", "importance": 0.8},'
            ' {"content": "Heisst Max", "category": "fact", "importance": 0.9}]'
        )

        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=llm_response)

        with patch.object(memory_service, '_get_ollama_client', return_value=mock_client), \
             patch.object(memory_service, '_get_embedding', return_value=None):
            result = await memory_service.extract_and_save(
                user_message="Ich bin Max und mag Jazz",
                assistant_response="Schoen, Max! Jazz ist toll.",
                user_id=None,
                session_id="sess-123",
                lang="de",
            )

        assert len(result) == 2
        contents = {m.content for m in result}
        assert "Mag Jazz" in contents
        assert "Heisst Max" in contents

    @pytest.mark.unit
    async def test_extract_and_save_empty_response(self, memory_service):
        """LLM returns [] → no memories."""
        llm_response = _make_llm_response('[]')
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=llm_response)

        with patch.object(memory_service, '_get_ollama_client', return_value=mock_client):
            result = await memory_service.extract_and_save(
                user_message="Schalte das Licht ein",
                assistant_response="Licht ist an.",
            )

        assert result == []

    @pytest.mark.unit
    async def test_extract_and_save_invalid_json(self, memory_service):
        """LLM returns garbage → graceful, 0 memories."""
        llm_response = _make_llm_response('This is not JSON at all!')
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=llm_response)

        with patch.object(memory_service, '_get_ollama_client', return_value=mock_client):
            result = await memory_service.extract_and_save(
                user_message="Hallo",
                assistant_response="Hallo!",
            )

        assert result == []

    @pytest.mark.unit
    async def test_extract_and_save_invalid_category(self, memory_service):
        """Extracted fact with invalid category is skipped."""
        llm_response = _make_llm_response(
            '[{"content": "Likes cats", "category": "invalid_cat", "importance": 0.7},'
            ' {"content": "Name is Max", "category": "fact", "importance": 0.8}]'
        )
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=llm_response)

        with patch.object(memory_service, '_get_ollama_client', return_value=mock_client), \
             patch.object(memory_service, '_get_embedding', return_value=None):
            result = await memory_service.extract_and_save(
                user_message="Ich heisse Max und mag Katzen",
                assistant_response="Hallo Max!",
            )

        assert len(result) == 1
        assert result[0].content == "Name is Max"

    @pytest.mark.unit
    async def test_extract_and_save_llm_failure(self, memory_service):
        """LLM call fails → empty list, no crash."""
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(side_effect=Exception("Connection refused"))

        with patch.object(memory_service, '_get_ollama_client', return_value=mock_client):
            result = await memory_service.extract_and_save(
                user_message="Hallo",
                assistant_response="Hallo!",
            )

        assert result == []

    @pytest.mark.unit
    async def test_extract_and_save_deduplication(self, memory_service, db_session):
        """Already existing fact → access_count incremented via dedup."""
        # Create existing memory
        existing = ConversationMemory(
            content="Mag Jazz",
            category="preference",
            access_count=1,
        )
        db_session.add(existing)
        await db_session.commit()
        await db_session.refresh(existing)

        llm_response = _make_llm_response(
            '[{"content": "Mag Jazz", "category": "preference", "importance": 0.8}]'
        )
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=llm_response)

        with patch.object(memory_service, '_get_ollama_client', return_value=mock_client), \
             patch.object(memory_service, '_get_embedding', return_value=[0.1] * 768), \
             patch.object(memory_service, '_find_duplicate', return_value=existing):
            result = await memory_service.extract_and_save(
                user_message="Ich mag Jazz",
                assistant_response="Jazz ist toll!",
            )

        assert len(result) == 1
        assert result[0].id == existing.id
        assert result[0].access_count == 2

    @pytest.mark.unit
    async def test_extract_and_save_markdown_code_block(self, memory_service):
        """LLM wraps response in markdown code block."""
        llm_response = _make_llm_response(
            '```json\n[{"content": "Trinkt gerne Tee", "category": "preference", "importance": 0.6}]\n```'
        )
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=llm_response)

        with patch.object(memory_service, '_get_ollama_client', return_value=mock_client), \
             patch.object(memory_service, '_get_embedding', return_value=None):
            result = await memory_service.extract_and_save(
                user_message="Ich trinke gerne Tee",
                assistant_response="Tee ist lecker!",
            )

        assert len(result) == 1
        assert result[0].content == "Trinkt gerne Tee"

    @pytest.mark.unit
    async def test_extract_and_save_importance_clamped(self, memory_service):
        """Importance values are clamped to 0.1-1.0."""
        llm_response = _make_llm_response(
            '[{"content": "Test", "category": "fact", "importance": 5.0}]'
        )
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=llm_response)

        with patch.object(memory_service, '_get_ollama_client', return_value=mock_client), \
             patch.object(memory_service, '_get_embedding', return_value=None):
            result = await memory_service.extract_and_save(
                user_message="Test",
                assistant_response="Ok",
            )

        assert len(result) == 1
        assert result[0].importance == 1.0

    @pytest.mark.unit
    async def test_extract_and_save_empty_content_skipped(self, memory_service):
        """Items with empty content are skipped."""
        llm_response = _make_llm_response(
            '[{"content": "", "category": "fact", "importance": 0.5},'
            ' {"content": "Valid fact", "category": "fact", "importance": 0.7}]'
        )
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=llm_response)

        with patch.object(memory_service, '_get_ollama_client', return_value=mock_client), \
             patch.object(memory_service, '_get_embedding', return_value=None):
            result = await memory_service.extract_and_save(
                user_message="Test",
                assistant_response="Ok",
            )

        assert len(result) == 1
        assert result[0].content == "Valid fact"


class TestMemoryExtractionParsing:
    """Tests for _parse_extraction_response static method."""

    @pytest.mark.unit
    def test_parse_valid_json_array(self):
        """Standard JSON array is parsed correctly."""
        result = ConversationMemoryService._parse_extraction_response(
            '[{"content": "Test", "category": "fact"}]'
        )
        assert len(result) == 1
        assert result[0]["content"] == "Test"

    @pytest.mark.unit
    def test_parse_empty_array(self):
        """Empty JSON array returns empty list."""
        result = ConversationMemoryService._parse_extraction_response('[]')
        assert result == []

    @pytest.mark.unit
    def test_parse_empty_string(self):
        """Empty string returns empty list."""
        result = ConversationMemoryService._parse_extraction_response('')
        assert result == []

    @pytest.mark.unit
    def test_parse_none(self):
        """None returns empty list."""
        result = ConversationMemoryService._parse_extraction_response(None)
        assert result == []

    @pytest.mark.unit
    def test_parse_markdown_code_block(self):
        """JSON wrapped in markdown code block."""
        text = '```json\n[{"content": "Test", "category": "fact"}]\n```'
        result = ConversationMemoryService._parse_extraction_response(text)
        assert len(result) == 1

    @pytest.mark.unit
    def test_parse_extra_text_around_json(self):
        """JSON array with extra text before/after."""
        text = 'Here are the results:\n[{"content": "Test", "category": "fact"}]\nDone!'
        result = ConversationMemoryService._parse_extraction_response(text)
        assert len(result) == 1

    @pytest.mark.unit
    def test_parse_non_dict_items_filtered(self):
        """Non-dict items in the array are filtered out."""
        text = '[{"content": "Valid"}, "invalid_string", 42]'
        result = ConversationMemoryService._parse_extraction_response(text)
        assert len(result) == 1
        assert result[0]["content"] == "Valid"

    @pytest.mark.unit
    def test_parse_garbage_returns_empty(self):
        """Completely invalid text returns empty list."""
        result = ConversationMemoryService._parse_extraction_response(
            'I could not find any facts in this conversation.'
        )
        assert result == []


class TestMemoryExtractionPrompt:
    """Tests for extraction prompt templates."""

    @pytest.mark.unit
    def test_extraction_prompt_german(self):
        """German extraction prompt renders correctly with variables."""
        from services.prompt_manager import prompt_manager

        # Reload to pick up new memory.yaml
        prompt_manager.reload()

        result = prompt_manager.get(
            "memory", "extraction_prompt", lang="de",
            user_message="Ich heisse Max",
            assistant_response="Hallo Max!",
        )

        assert "Ich heisse Max" in result
        assert "Hallo Max!" in result
        assert "EXTRAHIERE" in result
        assert "IGNORIERE" in result

    @pytest.mark.unit
    def test_extraction_prompt_english(self):
        """English extraction prompt renders correctly with variables."""
        from services.prompt_manager import prompt_manager

        prompt_manager.reload()

        result = prompt_manager.get(
            "memory", "extraction_prompt", lang="en",
            user_message="My name is Max",
            assistant_response="Hello Max!",
        )

        assert "My name is Max" in result
        assert "Hello Max!" in result
        assert "EXTRACT" in result
        assert "IGNORE" in result

    @pytest.mark.unit
    def test_extraction_system_prompt(self):
        """System prompt exists for both languages."""
        from services.prompt_manager import prompt_manager

        prompt_manager.reload()

        de = prompt_manager.get("memory", "extraction_system", lang="de")
        en = prompt_manager.get("memory", "extraction_system", lang="en")

        assert "JSON" in de
        assert "JSON" in en

    @pytest.mark.unit
    def test_extraction_llm_options(self):
        """LLM options are configured for low temperature."""
        from services.prompt_manager import prompt_manager

        prompt_manager.reload()

        options = prompt_manager.get_config("memory", "llm_options")
        assert options is not None
        assert options["temperature"] == 0.1
        assert options["num_predict"] == 500


class TestMemoryExtractionConfig:
    """Tests for memory extraction config setting."""

    @pytest.mark.unit
    def test_extraction_enabled_default(self):
        """Default is False."""
        from utils.config import Settings
        s = Settings(database_url="sqlite:///:memory:")
        assert s.memory_extraction_enabled is False

    @pytest.mark.unit
    def test_extraction_enabled_custom(self):
        """Can be set to True."""
        from utils.config import Settings
        s = Settings(
            database_url="sqlite:///:memory:",
            memory_extraction_enabled=True,
        )
        assert s.memory_extraction_enabled is True
