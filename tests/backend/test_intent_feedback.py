"""
Tests für Intent Feedback Service und API

Testet:
- IntentFeedbackService (save, find_similar, format, complexity override)
- Feedback API (POST/GET/DELETE corrections)
- ComplexityDetector with feedback integration
"""

import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from httpx import AsyncClient

from models.database import IntentCorrection


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def feedback_service(db_session: AsyncSession):
    """Create IntentFeedbackService with test database."""
    from services.intent_feedback_service import IntentFeedbackService
    service = IntentFeedbackService(db_session)
    # Clear count cache between tests
    IntentFeedbackService._count_cache.clear()
    return service


@pytest.fixture
async def sample_intent_correction(db_session: AsyncSession) -> IntentCorrection:
    """Create a sample intent correction in database."""
    correction = IntentCorrection(
        message_text="Was passierte 1989?",
        feedback_type="intent",
        original_value="knowledge.ask",
        corrected_value="general.conversation",
        user_id=None,
    )
    db_session.add(correction)
    await db_session.commit()
    await db_session.refresh(correction)
    return correction


@pytest.fixture
async def sample_agent_correction(db_session: AsyncSession) -> IntentCorrection:
    """Create a sample agent tool correction."""
    correction = IntentCorrection(
        message_text="Aktienkurs Tesla",
        feedback_type="agent_tool",
        original_value="mcp.weather",
        corrected_value="mcp.search.web",
        context={"step": 1, "agent_steps": ["mcp.weather"]},
    )
    db_session.add(correction)
    await db_session.commit()
    await db_session.refresh(correction)
    return correction


@pytest.fixture
async def sample_complexity_correction(db_session: AsyncSession) -> IntentCorrection:
    """Create a sample complexity correction."""
    correction = IntentCorrection(
        message_text="Wie ist das Wetter und such mir ein Hotel",
        feedback_type="complexity",
        original_value="simple",
        corrected_value="complex",
    )
    db_session.add(correction)
    await db_session.commit()
    await db_session.refresh(correction)
    return correction


@pytest.fixture
async def multiple_corrections(db_session: AsyncSession):
    """Create multiple corrections of different types."""
    corrections = [
        IntentCorrection(
            message_text="Was passierte 1989?",
            feedback_type="intent",
            original_value="knowledge.ask",
            corrected_value="general.conversation",
        ),
        IntentCorrection(
            message_text="Erzähl mir was über Berlin",
            feedback_type="intent",
            original_value="mcp.search.web",
            corrected_value="general.conversation",
        ),
        IntentCorrection(
            message_text="Aktienkurs Tesla",
            feedback_type="agent_tool",
            original_value="mcp.weather",
            corrected_value="mcp.search.web",
        ),
        IntentCorrection(
            message_text="Schalte Licht an und such Hotels",
            feedback_type="complexity",
            original_value="simple",
            corrected_value="complex",
        ),
    ]
    for c in corrections:
        db_session.add(c)
    await db_session.commit()
    return corrections


# ============================================================================
# IntentFeedbackService Tests
# ============================================================================

class TestIntentFeedbackService:
    """Tests for IntentFeedbackService core methods."""

    @pytest.mark.unit
    async def test_save_correction_intent(self, feedback_service, db_session):
        """Test saving an intent correction."""
        with patch.object(feedback_service, '_get_embedding', new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.1] * 768

            correction = await feedback_service.save_correction(
                message_text="Was passierte 1989?",
                feedback_type="intent",
                original_value="knowledge.ask",
                corrected_value="general.conversation",
            )

        assert correction.id is not None
        assert correction.message_text == "Was passierte 1989?"
        assert correction.feedback_type == "intent"
        assert correction.original_value == "knowledge.ask"
        assert correction.corrected_value == "general.conversation"

    @pytest.mark.unit
    async def test_save_correction_agent_tool(self, feedback_service):
        """Test saving an agent tool correction with context."""
        with patch.object(feedback_service, '_get_embedding', new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.2] * 768

            correction = await feedback_service.save_correction(
                message_text="Aktienkurs Tesla",
                feedback_type="agent_tool",
                original_value="mcp.weather",
                corrected_value="mcp.search.web",
                context={"step": 1},
            )

        assert correction.feedback_type == "agent_tool"
        assert correction.context == {"step": 1}

    @pytest.mark.unit
    async def test_save_correction_complexity(self, feedback_service):
        """Test saving a complexity correction."""
        with patch.object(feedback_service, '_get_embedding', new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.3] * 768

            correction = await feedback_service.save_correction(
                message_text="Wetter und Hotel suchen",
                feedback_type="complexity",
                original_value="simple",
                corrected_value="complex",
            )

        assert correction.feedback_type == "complexity"
        assert correction.corrected_value == "complex"

    @pytest.mark.unit
    async def test_save_correction_embedding_failure(self, feedback_service):
        """Test that correction is saved even when embedding generation fails."""
        with patch.object(feedback_service, '_get_embedding', new_callable=AsyncMock) as mock_embed:
            mock_embed.side_effect = Exception("Ollama not available")

            correction = await feedback_service.save_correction(
                message_text="Test message",
                feedback_type="intent",
                original_value="a",
                corrected_value="b",
            )

        assert correction.id is not None
        assert correction.embedding is None

    @pytest.mark.unit
    async def test_save_correction_invalidates_cache(self, feedback_service):
        """Test that saving a correction invalidates the count cache."""
        from services.intent_feedback_service import IntentFeedbackService
        IntentFeedbackService._count_cache["intent"] = (0, time.time())

        with patch.object(feedback_service, '_get_embedding', new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.1] * 768
            await feedback_service.save_correction(
                message_text="Test",
                feedback_type="intent",
                original_value="a",
                corrected_value="b",
            )

        assert "intent" not in IntentFeedbackService._count_cache

    @pytest.mark.unit
    async def test_has_corrections_empty(self, feedback_service):
        """Test _has_corrections returns False when no corrections exist."""
        result = await feedback_service._has_corrections("intent")
        assert result is False

    @pytest.mark.unit
    async def test_has_corrections_with_data(self, feedback_service, sample_intent_correction):
        """Test _has_corrections returns True when corrections exist."""
        result = await feedback_service._has_corrections("intent")
        assert result is True

    @pytest.mark.unit
    async def test_has_corrections_cache(self, feedback_service, sample_intent_correction):
        """Test that _has_corrections uses cache on second call."""
        from services.intent_feedback_service import IntentFeedbackService

        # First call populates cache
        await feedback_service._has_corrections("intent")
        assert "intent" in IntentFeedbackService._count_cache

        # Second call uses cache (won't hit DB)
        result = await feedback_service._has_corrections("intent")
        assert result is True

    @pytest.mark.unit
    async def test_find_similar_empty(self, feedback_service):
        """Test find_similar_corrections returns empty when no corrections exist."""
        result = await feedback_service.find_similar_corrections(
            "Test message", feedback_type="intent"
        )
        assert result == []

    # =========================================================================
    # Format Tests
    # =========================================================================

    @pytest.mark.unit
    async def test_format_as_few_shot_empty(self, feedback_service):
        """Test format_as_few_shot returns empty string for empty list."""
        result = feedback_service.format_as_few_shot([])
        assert result == ""

    @pytest.mark.unit
    async def test_format_as_few_shot_german(self, feedback_service):
        """Test format_as_few_shot generates German text."""
        corrections = [
            {
                "message_text": "Was passierte 1989?",
                "original_value": "knowledge.ask",
                "corrected_value": "general.conversation",
            }
        ]

        result = feedback_service.format_as_few_shot(corrections, lang="de")

        assert "LERNBEISPIELE" in result
        assert "Was passierte 1989?" in result
        assert "knowledge.ask" in result
        assert "general.conversation" in result
        assert "Falsch:" in result
        assert "Richtig:" in result

    @pytest.mark.unit
    async def test_format_as_few_shot_english(self, feedback_service):
        """Test format_as_few_shot generates English text."""
        corrections = [
            {
                "message_text": "What happened in 1989?",
                "original_value": "knowledge.ask",
                "corrected_value": "general.conversation",
            }
        ]

        result = feedback_service.format_as_few_shot(corrections, lang="en")

        assert "LEARNING EXAMPLES" in result
        assert "Wrong:" in result
        assert "Correct:" in result

    @pytest.mark.unit
    async def test_format_as_few_shot_multiple(self, feedback_service):
        """Test format_as_few_shot with multiple corrections."""
        corrections = [
            {"message_text": "Msg1", "original_value": "a", "corrected_value": "b"},
            {"message_text": "Msg2", "original_value": "c", "corrected_value": "d"},
        ]

        result = feedback_service.format_as_few_shot(corrections, lang="de")

        assert "Msg1" in result
        assert "Msg2" in result

    @pytest.mark.unit
    async def test_format_agent_corrections_empty(self, feedback_service):
        """Test format_agent_corrections returns empty string for empty list."""
        result = feedback_service.format_agent_corrections([])
        assert result == ""

    @pytest.mark.unit
    async def test_format_agent_corrections_german(self, feedback_service):
        """Test format_agent_corrections generates German text."""
        corrections = [
            {
                "message_text": "Aktienkurs Tesla",
                "original_value": "mcp.weather",
                "corrected_value": "mcp.search.web",
            }
        ]

        result = feedback_service.format_agent_corrections(corrections, lang="de")

        assert "TOOL-KORREKTUREN" in result
        assert "Aktienkurs Tesla" in result
        assert "mcp.search.web" in result
        assert "Verwende" in result

    @pytest.mark.unit
    async def test_format_agent_corrections_english(self, feedback_service):
        """Test format_agent_corrections generates English text."""
        corrections = [
            {
                "message_text": "Tesla stock price",
                "original_value": "mcp.weather",
                "corrected_value": "mcp.search.web",
            }
        ]

        result = feedback_service.format_agent_corrections(corrections, lang="en")

        assert "TOOL CORRECTIONS" in result
        assert "Use" in result

    # =========================================================================
    # Admin Operations
    # =========================================================================

    @pytest.mark.unit
    async def test_list_corrections(self, feedback_service, multiple_corrections):
        """Test listing corrections."""
        result = await feedback_service.list_corrections()

        assert len(result) == 4
        assert all("id" in c for c in result)
        assert all("feedback_type" in c for c in result)

    @pytest.mark.unit
    async def test_list_corrections_filter_by_type(self, feedback_service, multiple_corrections):
        """Test listing corrections filtered by feedback_type."""
        result = await feedback_service.list_corrections(feedback_type="intent")
        assert len(result) == 2

        result = await feedback_service.list_corrections(feedback_type="agent_tool")
        assert len(result) == 1

        result = await feedback_service.list_corrections(feedback_type="complexity")
        assert len(result) == 1

    @pytest.mark.unit
    async def test_list_corrections_pagination(self, feedback_service, multiple_corrections):
        """Test listing corrections with limit and offset."""
        result = await feedback_service.list_corrections(limit=2, offset=0)
        assert len(result) == 2

        result = await feedback_service.list_corrections(limit=2, offset=2)
        assert len(result) == 2

    @pytest.mark.unit
    async def test_delete_correction(self, feedback_service, sample_intent_correction):
        """Test deleting a correction."""
        success = await feedback_service.delete_correction(sample_intent_correction.id)
        assert success is True

        # Verify it's gone
        count = await feedback_service.get_correction_count()
        assert count == 0

    @pytest.mark.unit
    async def test_delete_correction_not_found(self, feedback_service):
        """Test deleting a non-existent correction."""
        success = await feedback_service.delete_correction(99999)
        assert success is False

    @pytest.mark.unit
    async def test_get_correction_count(self, feedback_service, multiple_corrections):
        """Test getting correction count."""
        count = await feedback_service.get_correction_count()
        assert count == 4

    @pytest.mark.unit
    async def test_get_correction_count_by_type(self, feedback_service, multiple_corrections):
        """Test getting correction count filtered by type."""
        count = await feedback_service.get_correction_count(feedback_type="intent")
        assert count == 2

        count = await feedback_service.get_correction_count(feedback_type="complexity")
        assert count == 1


# ============================================================================
# Feedback API Tests
# ============================================================================

class TestFeedbackAPI:
    """Tests for the /api/feedback/ REST API endpoints."""

    @pytest.mark.integration
    async def test_submit_correction(self, async_client: AsyncClient, db_session):
        """Test POST /api/feedback/correction"""
        with patch('services.intent_feedback_service.IntentFeedbackService._get_embedding',
                   new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.1] * 768

            response = await async_client.post("/api/feedback/correction", json={
                "message_text": "Was passierte 1989?",
                "feedback_type": "intent",
                "original_value": "knowledge.ask",
                "corrected_value": "general.conversation",
            })

        assert response.status_code == 200
        data = response.json()
        assert data["message_text"] == "Was passierte 1989?"
        assert data["feedback_type"] == "intent"
        assert "id" in data

    @pytest.mark.integration
    async def test_submit_correction_invalid_type(self, async_client: AsyncClient):
        """Test POST /api/feedback/correction with invalid feedback_type."""
        response = await async_client.post("/api/feedback/correction", json={
            "message_text": "Test",
            "feedback_type": "invalid_type",
            "original_value": "a",
            "corrected_value": "b",
        })

        assert response.status_code == 422

    @pytest.mark.integration
    async def test_submit_correction_missing_fields(self, async_client: AsyncClient):
        """Test POST /api/feedback/correction with missing required fields."""
        response = await async_client.post("/api/feedback/correction", json={
            "message_text": "Test",
        })

        assert response.status_code == 422

    @pytest.mark.integration
    async def test_list_corrections(self, async_client: AsyncClient, db_session):
        """Test GET /api/feedback/corrections"""
        # Create corrections first
        for i in range(3):
            correction = IntentCorrection(
                message_text=f"Message {i}",
                feedback_type="intent",
                original_value="a",
                corrected_value="b",
            )
            db_session.add(correction)
        await db_session.commit()

        response = await async_client.get("/api/feedback/corrections")

        assert response.status_code == 200
        data = response.json()
        assert "corrections" in data
        assert "total" in data
        assert data["total"] == 3

    @pytest.mark.integration
    async def test_list_corrections_filter(self, async_client: AsyncClient, db_session):
        """Test GET /api/feedback/corrections with type filter."""
        db_session.add(IntentCorrection(
            message_text="Intent", feedback_type="intent",
            original_value="a", corrected_value="b",
        ))
        db_session.add(IntentCorrection(
            message_text="Complexity", feedback_type="complexity",
            original_value="simple", corrected_value="complex",
        ))
        await db_session.commit()

        response = await async_client.get("/api/feedback/corrections?feedback_type=intent")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1

    @pytest.mark.integration
    async def test_list_corrections_invalid_type(self, async_client: AsyncClient):
        """Test GET /api/feedback/corrections with invalid type filter."""
        response = await async_client.get("/api/feedback/corrections?feedback_type=bogus")

        assert response.status_code == 400

    @pytest.mark.integration
    async def test_delete_correction(self, async_client: AsyncClient, db_session):
        """Test DELETE /api/feedback/corrections/{id}"""
        correction = IntentCorrection(
            message_text="To delete", feedback_type="intent",
            original_value="a", corrected_value="b",
        )
        db_session.add(correction)
        await db_session.commit()
        await db_session.refresh(correction)

        response = await async_client.delete(f"/api/feedback/corrections/{correction.id}")

        assert response.status_code == 200
        assert response.json()["success"] is True

    @pytest.mark.integration
    async def test_delete_correction_not_found(self, async_client: AsyncClient):
        """Test DELETE /api/feedback/corrections/{id} with non-existent ID."""
        response = await async_client.delete("/api/feedback/corrections/99999")

        assert response.status_code == 404


# ============================================================================
# ComplexityDetector with Feedback Tests
# ============================================================================

class TestComplexityDetectorWithFeedback:
    """Tests for ComplexityDetector.needs_agent_with_feedback()."""

    @pytest.mark.unit
    async def test_needs_agent_with_feedback_no_override(self):
        """Test that regex fallback is used when no feedback override exists."""
        from services.complexity_detector import ComplexityDetector

        mock_svc = AsyncMock()
        mock_svc.check_complexity_override = AsyncMock(return_value=None)

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch('services.database.AsyncSessionLocal', return_value=mock_ctx):
            with patch('services.intent_feedback_service.IntentFeedbackService', return_value=mock_svc):
                result = await ComplexityDetector.needs_agent_with_feedback("Wie ist das Wetter?")
                assert result is False

    @pytest.mark.unit
    async def test_needs_agent_with_feedback_override_complex(self):
        """Test that feedback override to 'complex' works."""
        from services.complexity_detector import ComplexityDetector

        mock_svc = AsyncMock()
        mock_svc.check_complexity_override = AsyncMock(return_value=True)

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch('services.database.AsyncSessionLocal', return_value=mock_ctx):
            with patch('services.intent_feedback_service.IntentFeedbackService', return_value=mock_svc):
                result = await ComplexityDetector.needs_agent_with_feedback("Wie ist das Wetter?")
                assert result is True

    @pytest.mark.unit
    async def test_needs_agent_with_feedback_override_simple(self):
        """Test that feedback override to 'simple' works."""
        from services.complexity_detector import ComplexityDetector

        mock_svc = AsyncMock()
        mock_svc.check_complexity_override = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch('services.database.AsyncSessionLocal', return_value=mock_ctx):
            with patch('services.intent_feedback_service.IntentFeedbackService', return_value=mock_svc):
                result = await ComplexityDetector.needs_agent_with_feedback(
                    "Wenn es kälter als 18 Grad ist, dann such mir ein Hotel"
                )
                assert result is False

    @pytest.mark.unit
    async def test_needs_agent_with_feedback_exception_fallback(self):
        """Test that regex fallback is used when feedback service fails."""
        from services.complexity_detector import ComplexityDetector

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=Exception("DB error"))
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch('services.database.AsyncSessionLocal', return_value=mock_ctx):
            result = await ComplexityDetector.needs_agent_with_feedback(
                "Wenn es regnet, dann mach das Licht an"
            )
            assert result is True

    @pytest.mark.unit
    async def test_needs_agent_with_feedback_simple_fallback(self):
        """Test regex fallback for simple message when service fails."""
        from services.complexity_detector import ComplexityDetector

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=Exception("DB error"))
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch('services.database.AsyncSessionLocal', return_value=mock_ctx):
            result = await ComplexityDetector.needs_agent_with_feedback("Hallo")
            assert result is False


# ============================================================================
# IntentCorrection Model Tests
# ============================================================================

class TestIntentCorrectionModel:
    """Tests for the IntentCorrection database model."""

    @pytest.mark.database
    async def test_create_intent_correction(self, db_session):
        """Test creating an IntentCorrection record."""
        correction = IntentCorrection(
            message_text="Test message",
            feedback_type="intent",
            original_value="knowledge.ask",
            corrected_value="general.conversation",
        )
        db_session.add(correction)
        await db_session.commit()
        await db_session.refresh(correction)

        assert correction.id is not None
        assert correction.created_at is not None

    @pytest.mark.database
    async def test_create_correction_with_context(self, db_session):
        """Test creating a correction with JSON context."""
        correction = IntentCorrection(
            message_text="Test",
            feedback_type="agent_tool",
            original_value="mcp.weather",
            corrected_value="mcp.search.web",
            context={"step": 2, "agent_steps": ["mcp.weather", "mcp.search.web"]},
        )
        db_session.add(correction)
        await db_session.commit()
        await db_session.refresh(correction)

        assert correction.context["step"] == 2
        assert len(correction.context["agent_steps"]) == 2

    @pytest.mark.database
    async def test_create_correction_with_user(self, db_session, test_user):
        """Test creating a correction linked to a user."""
        correction = IntentCorrection(
            message_text="Test",
            feedback_type="intent",
            original_value="a",
            corrected_value="b",
            user_id=test_user.id,
        )
        db_session.add(correction)
        await db_session.commit()
        await db_session.refresh(correction)

        assert correction.user_id == test_user.id

    @pytest.mark.database
    async def test_query_by_feedback_type(self, db_session, multiple_corrections):
        """Test querying corrections by feedback_type."""
        result = await db_session.execute(
            select(IntentCorrection).where(IntentCorrection.feedback_type == "intent")
        )
        intent_corrections = result.scalars().all()
        assert len(intent_corrections) == 2

    @pytest.mark.database
    async def test_correction_defaults(self, db_session):
        """Test that defaults are set correctly."""
        correction = IntentCorrection(
            message_text="Test",
            feedback_type="intent",
            original_value="a",
            corrected_value="b",
        )
        db_session.add(correction)
        await db_session.commit()
        await db_session.refresh(correction)

        assert correction.embedding is None
        assert correction.context is None
        assert correction.user_id is None
