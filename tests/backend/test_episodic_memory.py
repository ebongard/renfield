"""
Tests for EpisodicMemoryService — Episode creation, retrieval, summarization, and cleanup.

Uses in-memory SQLite (no pgvector). Embedding generation is mocked.
Actual similarity search requires PostgreSQL and is covered by e2e tests.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    EMBEDDING_DIMENSION,
    MEMORY_CATEGORY_FACT,
    ConversationMemory,
    EpisodicMemory,
)
from services.episodic_memory_service import EpisodicMemoryService


# ==========================================================================
# Fixtures
# ==========================================================================


@pytest.fixture
def mock_embedding():
    """Mock embedding vector.

    Sized to ``EMBEDDING_DIMENSION`` (``settings.embedding_dimension``) —
    the EpisodicMemory.embedding pgvector column is created as
    ``Vector(EMBEDDING_DIMENSION)``, so a fixed-768 vector trips
    pgvector's dimensionality check when the deployment uses a different
    embedding model (production: 2560-dim qwen3-embedding:4b)."""
    return [0.1] * EMBEDDING_DIMENSION


@pytest.fixture
def ep_service(db_session: AsyncSession, mock_embedding):
    """EpisodicMemoryService with mocked embedding generation."""
    svc = EpisodicMemoryService(db_session)
    svc._get_embedding = AsyncMock(return_value=mock_embedding)
    return svc


# ==========================================================================
# create_episode()
# ==========================================================================


class TestCreateEpisode:
    """Tests for episode creation."""

    @pytest.mark.unit
    async def test_create_basic_episode(self, ep_service, db_session, test_user):
        """Happy path: create an episode with all fields."""
        episode = await ep_service.create_episode(
            user_id=test_user.id,
            session_id="session-123",
            summary="User asked: show releases | Tools: get_release | Outcome: success",
            topic="release_details",
            entities={"release_id": "Release-1"},
            tools_used=["get_release"],
            outcome="success",
            importance=0.6,
        )

        assert episode is not None
        assert episode.id is not None
        assert episode.user_id == test_user.id
        assert episode.session_id == "session-123"
        assert episode.summary.startswith("User asked:")
        assert episode.topic == "release_details"
        assert episode.entities == {"release_id": "Release-1"}
        assert episode.tools_used == ["get_release"]
        assert episode.outcome == "success"
        assert episode.importance == 0.6
        assert episode.is_active is True
        assert episode.access_count == 0

    @pytest.mark.unit
    async def test_create_episode_embedding_failure(self, ep_service, db_session, test_user):
        """Episode is created even when embedding generation fails."""
        ep_service._get_embedding = AsyncMock(side_effect=Exception("Ollama down"))

        episode = await ep_service.create_episode(
            user_id=test_user.id,
            session_id="session-456",
            summary="User asked something",
            topic="general",
        )

        assert episode is not None
        assert episode.embedding is None

    @pytest.mark.unit
    async def test_create_episode_no_user(self, ep_service, db_session):
        """Episode can be created without user_id."""
        episode = await ep_service.create_episode(
            user_id=None,
            session_id="anon-session",
            summary="Anonymous interaction",
        )

        assert episode is not None
        assert episode.user_id is None

    @pytest.mark.unit
    async def test_per_user_limit_deactivates_oldest(self, ep_service, db_session, test_user):
        """When at limit, oldest lowest-importance episode is deactivated."""
        with patch("services.episodic_memory_service.settings") as mock_settings:
            mock_settings.memory_episodic_max_per_user = 3
            mock_settings.ollama_embed_model = "nomic-embed-text"

            # Create 3 episodes (at limit)
            episodes = []
            for i in range(3):
                ep = await ep_service.create_episode(
                    user_id=test_user.id,
                    session_id=f"session-{i}",
                    summary=f"Episode {i}",
                    importance=0.5 + i * 0.1,
                )
                episodes.append(ep)

            # All 3 should be active
            result = await db_session.execute(
                select(func.count(EpisodicMemory.id))
                .where(
                    EpisodicMemory.user_id == test_user.id,
                    EpisodicMemory.is_active == True,  # noqa: E712
                )
            )
            assert result.scalar() == 3

            # Create a 4th — should deactivate the oldest (lowest importance)
            await ep_service.create_episode(
                user_id=test_user.id,
                session_id="session-3",
                summary="Episode 3",
                importance=0.8,
            )

            # Still 3 active
            result = await db_session.execute(
                select(func.count(EpisodicMemory.id))
                .where(
                    EpisodicMemory.user_id == test_user.id,
                    EpisodicMemory.is_active == True,  # noqa: E712
                )
            )
            assert result.scalar() == 3

            # The first episode (lowest importance) should be deactivated
            await db_session.refresh(episodes[0])
            assert episodes[0].is_active is False


# ==========================================================================
# retrieve_recent()
# ==========================================================================


class TestRetrieveRecent:
    """Tests for recent episode retrieval (no embedding needed)."""

    @pytest.mark.unit
    async def test_retrieve_recent_ordering(self, ep_service, db_session, test_user):
        """Returns episodes ordered by created_at DESC."""
        for i in range(5):
            await ep_service.create_episode(
                user_id=test_user.id,
                session_id=f"session-{i}",
                summary=f"Episode {i}",
                topic="general",
            )

        recent = await ep_service.retrieve_recent(user_id=test_user.id, limit=3)

        assert len(recent) == 3
        assert recent[0]["summary"] == "Episode 4"
        assert recent[1]["summary"] == "Episode 3"
        assert recent[2]["summary"] == "Episode 2"

    @pytest.mark.unit
    async def test_retrieve_recent_excludes_inactive(self, ep_service, db_session, test_user):
        """Inactive episodes are not returned."""
        ep = await ep_service.create_episode(
            user_id=test_user.id,
            session_id="session-1",
            summary="Active episode",
        )
        inactive = await ep_service.create_episode(
            user_id=test_user.id,
            session_id="session-2",
            summary="Inactive episode",
        )
        inactive.is_active = False
        await db_session.commit()

        recent = await ep_service.retrieve_recent(user_id=test_user.id)
        summaries = [r["summary"] for r in recent]
        assert "Active episode" in summaries
        assert "Inactive episode" not in summaries

    @pytest.mark.unit
    async def test_retrieve_recent_empty(self, ep_service, test_user):
        """Returns empty list when no episodes exist."""
        recent = await ep_service.retrieve_recent(user_id=test_user.id)
        assert recent == []

    @pytest.mark.unit
    async def test_retrieve_recent_respects_limit(self, ep_service, db_session, test_user):
        """Limit parameter controls max results."""
        for i in range(10):
            await ep_service.create_episode(
                user_id=test_user.id,
                session_id=f"session-{i}",
                summary=f"Episode {i}",
            )

        recent = await ep_service.retrieve_recent(user_id=test_user.id, limit=2)
        assert len(recent) == 2


# ==========================================================================
# summarize_old()
# ==========================================================================


class TestSummarizeOld:
    """Tests for batch summarization of old episodes into semantic facts."""

    @pytest.mark.unit
    async def test_summarize_below_threshold_noop(self, ep_service, db_session, test_user):
        """No summarization when episode count <= threshold."""
        with patch("services.episodic_memory_service.settings") as mock_settings:
            mock_settings.memory_episodic_summarize_threshold = 50
            mock_settings.memory_episodic_decay_days = 90
            mock_settings.ollama_embed_model = "nomic-embed-text"
            # create_episode reads this for the per-user cap check; left as a
            # bare MagicMock it breaks `count >= settings.<field>`.
            mock_settings.memory_episodic_max_per_user = 1000

            # Create 5 episodes (well below threshold of 50)
            for i in range(5):
                await ep_service.create_episode(
                    user_id=test_user.id,
                    session_id=f"session-{i}",
                    summary=f"Episode {i}",
                )

            result = await ep_service.summarize_old(user_id=test_user.id)
            assert result == 0

    @pytest.mark.skip(
        reason="summarize_old persists the derived fact via ConversationMemory "
        "dedup, which runs a pgvector `embedding <=> CAST(? AS vector)` cosine "
        "query — SQLite cannot parse the `<=>` operator. Needs a real Postgres "
        "engine; covered by the e2e suite."
    )
    @pytest.mark.unit
    async def test_summarize_creates_facts_and_deactivates(self, ep_service, db_session, test_user):
        """Old episodes are summarized into semantic facts and deactivated."""
        with patch("services.episodic_memory_service.settings") as mock_settings:
            mock_settings.memory_episodic_summarize_threshold = 3
            mock_settings.memory_episodic_decay_days = 0  # All episodes are "old"
            mock_settings.ollama_embed_model = "nomic-embed-text"
            mock_settings.memory_episodic_max_per_user = 1000

            # Create 5 episodes with same topic
            for i in range(5):
                ep = await ep_service.create_episode(
                    user_id=test_user.id,
                    session_id=f"session-{i}",
                    summary=f"Episode {i} about releases",
                    topic="release_details",
                )

            summarized = await ep_service.summarize_old(user_id=test_user.id)

            # Should summarize 2 episodes (5 total - 3 threshold = 2)
            assert summarized == 2

            # Check a fact was created in conversation_memories
            result = await db_session.execute(
                select(func.count(ConversationMemory.id))
                .where(
                    ConversationMemory.user_id == test_user.id,
                    ConversationMemory.category == MEMORY_CATEGORY_FACT,
                )
            )
            assert result.scalar() >= 1

    @pytest.mark.unit
    async def test_summarize_groups_by_topic(self, ep_service, db_session, test_user):
        """Episodes are grouped by topic for coherent summaries."""
        with patch("services.episodic_memory_service.settings") as mock_settings:
            mock_settings.memory_episodic_summarize_threshold = 2
            mock_settings.memory_episodic_decay_days = 0
            mock_settings.ollama_embed_model = "nomic-embed-text"
            mock_settings.memory_episodic_max_per_user = 1000

            # Create episodes with different topics
            for topic in ["release_details", "release_details", "jira_search", "jira_search"]:
                await ep_service.create_episode(
                    user_id=test_user.id,
                    session_id="session",
                    summary=f"About {topic}",
                    topic=topic,
                )

            summarized = await ep_service.summarize_old(user_id=test_user.id)
            assert summarized == 2  # 4 - 2 threshold = 2

            # Check facts were created (should be topic-grouped)
            result = await db_session.execute(
                select(ConversationMemory.content)
                .where(
                    ConversationMemory.user_id == test_user.id,
                    ConversationMemory.category == MEMORY_CATEGORY_FACT,
                )
            )
            facts = [row[0] for row in result.fetchall()]
            assert len(facts) >= 1


# ==========================================================================
# cleanup()
# ==========================================================================


class TestEpisodicCleanup:
    """Tests for episodic memory cleanup."""

    @pytest.mark.unit
    async def test_cleanup_decays_old_accessed_episodes(self, ep_service, db_session, test_user):
        """Episodes not accessed within decay period are deactivated."""
        with patch("services.episodic_memory_service.settings") as mock_settings:
            mock_settings.memory_episodic_decay_days = 90
            mock_settings.ollama_embed_model = "nomic-embed-text"
            mock_settings.memory_episodic_max_per_user = 1000

            ep = await ep_service.create_episode(
                user_id=test_user.id,
                session_id="old-session",
                summary="Old episode",
            )
            # Set last_accessed_at to 100 days ago
            ep.last_accessed_at = datetime.utcnow() - timedelta(days=100)
            await db_session.commit()

            counts = await ep_service.cleanup()
            assert counts["decayed"] >= 1

            await db_session.refresh(ep)
            assert ep.is_active is False

    @pytest.mark.unit
    async def test_cleanup_deactivates_very_old_episodes(self, ep_service, db_session, test_user):
        """Episodes older than 365 days are deactivated regardless."""
        with patch("services.episodic_memory_service.settings") as mock_settings:
            mock_settings.memory_episodic_decay_days = 90
            mock_settings.ollama_embed_model = "nomic-embed-text"
            mock_settings.memory_episodic_max_per_user = 1000

            ep = await ep_service.create_episode(
                user_id=test_user.id,
                session_id="ancient-session",
                summary="Ancient episode",
            )
            ep.created_at = datetime.utcnow() - timedelta(days=400)
            await db_session.commit()

            counts = await ep_service.cleanup()
            assert counts["old"] >= 1

            await db_session.refresh(ep)
            assert ep.is_active is False

    @pytest.mark.unit
    async def test_cleanup_noop_when_fresh(self, ep_service, db_session, test_user):
        """No deactivation when all episodes are fresh."""
        with patch("services.episodic_memory_service.settings") as mock_settings:
            mock_settings.memory_episodic_decay_days = 90
            mock_settings.ollama_embed_model = "nomic-embed-text"
            mock_settings.memory_episodic_max_per_user = 1000

            await ep_service.create_episode(
                user_id=test_user.id,
                session_id="fresh-session",
                summary="Fresh episode",
            )

            counts = await ep_service.cleanup()
            assert counts["decayed"] == 0
            assert counts["old"] == 0


# ==========================================================================
# _deactivate_oldest()
# ==========================================================================


class TestDeactivateOldest:
    """Tests for the internal _deactivate_oldest helper."""

    @pytest.mark.unit
    async def test_deactivates_lowest_importance_first(self, ep_service, db_session, test_user):
        """Deactivates by importance ASC, then created_at ASC."""
        ep_high = await ep_service.create_episode(
            user_id=test_user.id,
            session_id="high",
            summary="High importance",
            importance=0.9,
        )
        ep_low = await ep_service.create_episode(
            user_id=test_user.id,
            session_id="low",
            summary="Low importance",
            importance=0.1,
        )

        await ep_service._deactivate_oldest(test_user.id)

        await db_session.refresh(ep_high)
        await db_session.refresh(ep_low)
        assert ep_high.is_active is True
        assert ep_low.is_active is False

    @pytest.mark.unit
    async def test_deactivate_oldest_noop_when_empty(self, ep_service, db_session, test_user):
        """No error when user has no episodes."""
        await ep_service._deactivate_oldest(test_user.id)
        # Should not raise
