"""Tests for async TaskQueue service.

Tests enqueue, dequeue, status tracking, queue length, and error handling
against the async Redis-backed implementation.
"""
import json
import sys
from unittest.mock import MagicMock

# Pre-mock modules not available in test environment
_missing_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
    "redis", "redis.asyncio",
]
for _mod in _missing_stubs:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from unittest.mock import AsyncMock, patch

import pytest

from services.task_queue import TaskQueue


@pytest.fixture
def mock_redis():
    """Create a mock async Redis client."""
    client = AsyncMock()
    client.incr = AsyncMock(return_value=1)
    client.lpush = AsyncMock()
    client.rpop = AsyncMock(return_value=None)
    client.get = AsyncMock(return_value=None)
    client.set = AsyncMock()
    client.llen = AsyncMock(return_value=0)
    client.close = AsyncMock()
    return client


@pytest.fixture
def queue(mock_redis):
    with patch("services.task_queue.aioredis") as mock_aioredis:
        mock_aioredis.from_url.return_value = mock_redis
        with patch("services.task_queue.settings") as mock_s:
            mock_s.redis_url = "redis://localhost:6379"
            q = TaskQueue()
    return q


# ============================================================================
# Initialization Tests
# ============================================================================

@pytest.mark.unit
class TestTaskQueueInit:

    def test_queue_name(self, queue):
        assert queue.queue_name == "renfield:tasks"

    def test_redis_client_created(self, queue):
        assert queue.redis_client is not None


# ============================================================================
# Enqueue Tests
# ============================================================================

@pytest.mark.unit
class TestEnqueue:

    @pytest.mark.asyncio
    async def test_enqueue_returns_task_id(self, queue, mock_redis):
        """Enqueue returns a unique task ID."""
        task_id = await queue.enqueue("process_document", {"file": "test.pdf"})

        assert task_id == "task:process_document:1"
        mock_redis.incr.assert_called_once_with("task:counter")

    @pytest.mark.asyncio
    async def test_enqueue_pushes_to_list(self, queue, mock_redis):
        """Task is pushed to the Redis list."""
        await queue.enqueue("process_document", {"file": "test.pdf"})

        mock_redis.lpush.assert_called_once()
        call_args = mock_redis.lpush.call_args[0]
        assert call_args[0] == "renfield:tasks"
        task_data = json.loads(call_args[1])
        assert task_data["type"] == "process_document"
        assert task_data["status"] == "queued"
        assert task_data["parameters"] == {"file": "test.pdf"}

    @pytest.mark.asyncio
    async def test_enqueue_stores_task_by_id(self, queue, mock_redis):
        """Task is also stored by its ID for status lookups."""
        await queue.enqueue("embed", {"doc_id": 42})

        # set should be called with the task ID as key
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args[0]
        assert call_args[0] == "task:embed:1"
        task_data = json.loads(call_args[1])
        assert task_data["id"] == "task:embed:1"

    @pytest.mark.asyncio
    async def test_enqueue_increments_counter(self, queue, mock_redis):
        """Each enqueue increments the counter."""
        mock_redis.incr = AsyncMock(side_effect=[1, 2, 3])

        id1 = await queue.enqueue("a", {})
        id2 = await queue.enqueue("b", {})
        id3 = await queue.enqueue("c", {})

        assert id1 == "task:a:1"
        assert id2 == "task:b:2"
        assert id3 == "task:c:3"

    @pytest.mark.asyncio
    async def test_enqueue_raises_on_redis_error(self, queue, mock_redis):
        """Redis errors propagate from enqueue."""
        mock_redis.incr = AsyncMock(side_effect=Exception("Connection refused"))

        with pytest.raises(Exception, match="Connection refused"):
            await queue.enqueue("task", {})


# ============================================================================
# Dequeue Tests
# ============================================================================

@pytest.mark.unit
class TestDequeue:

    @pytest.mark.asyncio
    async def test_dequeue_returns_task(self, queue, mock_redis):
        """Dequeue returns a task dict when queue is non-empty."""
        task_data = {"id": "task:test:1", "type": "test", "parameters": {}, "status": "queued"}
        mock_redis.rpop = AsyncMock(return_value=json.dumps(task_data))

        result = await queue.dequeue()

        assert result == task_data
        mock_redis.rpop.assert_called_once_with("renfield:tasks")

    @pytest.mark.asyncio
    async def test_dequeue_returns_none_when_empty(self, queue, mock_redis):
        """Dequeue returns None when queue is empty."""
        mock_redis.rpop = AsyncMock(return_value=None)

        result = await queue.dequeue()

        assert result is None

    @pytest.mark.asyncio
    async def test_dequeue_returns_none_on_error(self, queue, mock_redis):
        """Redis errors return None instead of raising."""
        mock_redis.rpop = AsyncMock(side_effect=Exception("timeout"))

        result = await queue.dequeue()

        assert result is None


# ============================================================================
# Task Status Tests
# ============================================================================

@pytest.mark.unit
class TestTaskStatus:

    @pytest.mark.asyncio
    async def test_get_task_status_returns_data(self, queue, mock_redis):
        """Get status returns task data when task exists."""
        task_data = {"id": "task:test:1", "type": "test", "status": "queued"}
        mock_redis.get = AsyncMock(return_value=json.dumps(task_data))

        result = await queue.get_task_status("task:test:1")

        assert result == task_data

    @pytest.mark.asyncio
    async def test_get_task_status_returns_none_for_unknown(self, queue, mock_redis):
        """Returns None for unknown task IDs."""
        mock_redis.get = AsyncMock(return_value=None)

        result = await queue.get_task_status("task:nonexistent:99")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_task_status_returns_none_on_error(self, queue, mock_redis):
        """Redis errors return None."""
        mock_redis.get = AsyncMock(side_effect=Exception("connection lost"))

        result = await queue.get_task_status("task:test:1")

        assert result is None

    @pytest.mark.asyncio
    async def test_update_task_status(self, queue, mock_redis):
        """Task status is updated in Redis."""
        existing = {"id": "task:test:1", "type": "test", "status": "queued"}
        mock_redis.get = AsyncMock(return_value=json.dumps(existing))

        await queue.update_task_status("task:test:1", "processing")

        mock_redis.set.assert_called_once()
        updated = json.loads(mock_redis.set.call_args[0][1])
        assert updated["status"] == "processing"

    @pytest.mark.asyncio
    async def test_update_task_status_with_result(self, queue, mock_redis):
        """Task status update includes result data."""
        existing = {"id": "task:test:1", "type": "test", "status": "processing"}
        mock_redis.get = AsyncMock(return_value=json.dumps(existing))

        await queue.update_task_status(
            "task:test:1", "completed", result={"chunks": 5}
        )

        updated = json.loads(mock_redis.set.call_args[0][1])
        assert updated["status"] == "completed"
        assert updated["result"] == {"chunks": 5}

    @pytest.mark.asyncio
    async def test_update_nonexistent_task_is_noop(self, queue, mock_redis):
        """Updating a non-existent task does nothing."""
        mock_redis.get = AsyncMock(return_value=None)

        await queue.update_task_status("task:missing:99", "completed")

        mock_redis.set.assert_not_called()


# ============================================================================
# Queue Length Tests
# ============================================================================

@pytest.mark.unit
class TestQueueLength:

    @pytest.mark.asyncio
    async def test_queue_length_empty(self, queue, mock_redis):
        mock_redis.llen = AsyncMock(return_value=0)

        length = await queue.queue_length()

        assert length == 0
        mock_redis.llen.assert_called_once_with("renfield:tasks")

    @pytest.mark.asyncio
    async def test_queue_length_with_items(self, queue, mock_redis):
        mock_redis.llen = AsyncMock(return_value=5)

        length = await queue.queue_length()

        assert length == 5


# ============================================================================
# Close Tests
# ============================================================================

@pytest.mark.unit
class TestClose:

    @pytest.mark.asyncio
    async def test_close_closes_redis(self, queue, mock_redis):
        await queue.close()

        mock_redis.close.assert_called_once()
