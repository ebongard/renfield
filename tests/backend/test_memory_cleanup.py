"""
Tests for Memory Cleanup Scheduler, Config, and Metrics Integration.

Covers:
- memory_cleanup_interval config setting
- Prometheus metrics calls from cleanup()
- Background scheduler creation based on memory_enabled flag
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import MEMORY_CATEGORY_CONTEXT, ConversationMemory

# ==========================================================================
# Config Tests
# ==========================================================================


class TestMemoryCleanupConfig:
    """Tests for memory_cleanup_interval setting."""

    @pytest.mark.unit
    def test_cleanup_interval_default(self):
        """Default cleanup interval is 3600 seconds (1 hour)."""
        from utils.config import Settings

        s = Settings(database_url="postgresql://x:x@localhost/test")
        assert s.memory_cleanup_interval == 3600

    @pytest.mark.unit
    def test_cleanup_interval_custom(self):
        """Cleanup interval can be set via env var."""
        from utils.config import Settings

        s = Settings(
            database_url="postgresql://x:x@localhost/test",
            memory_cleanup_interval=300,
        )
        assert s.memory_cleanup_interval == 300


# ==========================================================================
# Metrics Integration Tests
# ==========================================================================


class TestMemoryCleanupMetrics:
    """Tests for Prometheus metrics integration in cleanup()."""

    @pytest.mark.unit
    async def test_record_memory_cleanup_called(self, db_session: AsyncSession):
        """record_memory_cleanup is called after cleanup with counts."""
        memory = ConversationMemory(
            content="Expiring memory",
            category=MEMORY_CATEGORY_CONTEXT,
            is_active=True,
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        db_session.add(memory)
        await db_session.commit()

        from services.conversation_memory_service import ConversationMemoryService

        service = ConversationMemoryService(db_session)

        with patch("utils.metrics.record_memory_cleanup") as mock_record, \
             patch("utils.metrics.set_memory_total"):
            counts = await service.cleanup()
            assert counts["expired"] >= 1
            mock_record.assert_called_once_with(counts)

    @pytest.mark.unit
    async def test_set_memory_total_called(self, db_session: AsyncSession):
        """set_memory_total is called after cleanup."""
        memory = ConversationMemory(
            content="Another expiring memory",
            category=MEMORY_CATEGORY_CONTEXT,
            is_active=True,
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        db_session.add(memory)
        await db_session.commit()

        from services.conversation_memory_service import ConversationMemoryService

        service = ConversationMemoryService(db_session)

        with patch("utils.metrics.record_memory_cleanup"), \
             patch("utils.metrics.set_memory_total") as mock_total:
            await service.cleanup()
            mock_total.assert_called_once()
            # Should be called with an integer (active count)
            args = mock_total.call_args[0]
            assert isinstance(args[0], int)

    @pytest.mark.unit
    async def test_metrics_failure_does_not_break_cleanup(
        self, db_session: AsyncSession
    ):
        """If metrics raise, cleanup still returns valid counts."""
        memory = ConversationMemory(
            content="Yet another expiring memory",
            category=MEMORY_CATEGORY_CONTEXT,
            is_active=True,
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        db_session.add(memory)
        await db_session.commit()

        from services.conversation_memory_service import ConversationMemoryService

        service = ConversationMemoryService(db_session)

        with patch(
            "utils.metrics.record_memory_cleanup",
            side_effect=RuntimeError("metrics broken"),
        ):
            counts = await service.cleanup()
            # Cleanup should succeed despite metrics error
            assert isinstance(counts, dict)
            assert counts["expired"] >= 1


# ==========================================================================
# Scheduler Tests
# ==========================================================================


def _mock_lifecycle_modules():
    """Context manager to mock heavy dependencies for lifecycle import."""
    mocks = {
        "services.database": MagicMock(),
        "ha_glue.services.device_manager": MagicMock(),
        "services.ollama_service": MagicMock(),
        "services.task_queue": MagicMock(),
        "redis": MagicMock(),
    }
    return patch.dict("sys.modules", mocks)


class TestMemoryCleanupScheduler:
    """The memory-cleanup scheduler was migrated to the Scheduled Tasks engine
    (#1137); its logic now lives in ``services.scheduled_tasks.builtins.
    _memory_cleanup_handler``. The gate (memory_enabled) re-assertion is verified
    here; the enabled-path cleanup logic is covered by the ConversationMemoryService
    tests above and the engine's execution tests."""

    @pytest.mark.unit
    async def test_handler_skips_when_disabled(self, monkeypatch):
        from types import SimpleNamespace

        from services.scheduled_tasks import builtins
        from utils.config import settings

        monkeypatch.setattr(settings, "memory_enabled", False)
        out = await builtins._memory_cleanup_handler(SimpleNamespace(state=SimpleNamespace()), {})
        assert out is not None and "skipped" in out
