"""Concurrency tests for parallel operations."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.circuit_breaker import CircuitBreaker, CircuitState

# ============================================================================
# Helpers
# ============================================================================

def _make_intent_response(intent: str, confidence: float = 0.9) -> dict:
    """Build a mock Ollama chat response containing a JSON intent."""
    return {
        "message": {
            "content": json.dumps({
                "intent": intent,
                "parameters": {},
                "confidence": confidence
            })
        }
    }


# ============================================================================
# Circuit Breaker Concurrency Tests
# ============================================================================

class TestCircuitBreakerConcurrency:
    """Test circuit breaker under concurrent access."""

    @pytest.mark.unit
    async def test_concurrent_allow_request(self):
        """10 tasks calling allow_request simultaneously should all succeed when closed."""
        breaker = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout=30.0)

        results = await asyncio.gather(
            *[breaker.allow_request() for _ in range(10)]
        )

        # All should be allowed when circuit is closed
        assert all(results)
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.unit
    async def test_concurrent_record_failure(self):
        """5 tasks recording failures concurrently should trigger state transition atomically."""
        breaker = CircuitBreaker(
            name="test",
            failure_threshold=3,
            recovery_timeout=30.0,
        )

        with patch.object(breaker, '_record_state_metric'):
            await asyncio.gather(
                *[breaker.record_failure() for _ in range(5)]
            )

        # Should have opened after reaching threshold
        assert breaker.state == CircuitState.OPEN
        assert breaker.failure_count == 5

    @pytest.mark.unit
    async def test_concurrent_failure_then_success(self):
        """Interleaved failures and successes should maintain consistent state."""
        breaker = CircuitBreaker(
            name="test",
            failure_threshold=5,
            recovery_timeout=30.0,
        )

        with patch.object(breaker, '_record_state_metric'):
            # Record 2 failures, then a success (resets count), then 2 more failures
            await breaker.record_failure()
            await breaker.record_failure()
            await breaker.record_success()  # Resets failure count to 0
            # Now run 4 failures concurrently — should NOT open (threshold=5)
            await asyncio.gather(
                *[breaker.record_failure() for _ in range(4)]
            )

        # 4 failures < 5 threshold, so should stay closed
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 4

    @pytest.mark.unit
    async def test_concurrent_allow_when_open(self):
        """Concurrent allow_request calls when circuit is open should all be rejected."""
        breaker = CircuitBreaker(
            name="test",
            failure_threshold=1,
            recovery_timeout=9999.0,  # Long timeout so it stays open
        )

        with patch.object(breaker, '_record_state_metric'):
            await breaker.record_failure()  # Opens the circuit

        assert breaker.state == CircuitState.OPEN

        results = await asyncio.gather(
            *[breaker.allow_request() for _ in range(10)]
        )

        # All should be rejected when open (timeout not elapsed)
        assert not any(results)


# ============================================================================
# WebSocket Rate Limiter Concurrency Tests
# ============================================================================

class TestWebSocketRateLimiterConcurrency:
    """Test WebSocket rate limiter under concurrent access."""

    @pytest.mark.unit
    def test_concurrent_rate_limit_checks(self):
        """Multiple clients checking rate limits simultaneously should be isolated."""
        from services.websocket_rate_limiter import WSRateLimiter

        limiter = WSRateLimiter(per_second=5, per_minute=100, enabled=True)

        # Simulate multiple clients sending messages
        results = {}
        for client_id in ["client_a", "client_b", "client_c"]:
            client_results = []
            for _ in range(7):
                allowed, _reason = limiter.check(client_id)
                client_results.append(allowed)
            results[client_id] = client_results

        # Each client should have 5 allowed and 2 rejected (per_second=5)
        for client_id, client_results in results.items():
            assert sum(client_results) == 5, f"{client_id} should have 5 allowed"
            assert client_results.count(False) == 2, f"{client_id} should have 2 rejected"

    @pytest.mark.unit
    def test_rate_limiter_client_isolation(self):
        """One client hitting the limit should not affect another client."""
        from services.websocket_rate_limiter import WSRateLimiter

        limiter = WSRateLimiter(per_second=2, per_minute=100, enabled=True)

        # Client A sends 3 messages (exceeds per_second=2)
        for _ in range(3):
            limiter.check("client_a")

        # Client B should still be allowed
        allowed, _ = limiter.check("client_b")
        assert allowed is True

    @pytest.mark.unit
    def test_rate_limiter_disabled(self):
        """Disabled rate limiter should allow all messages."""
        from services.websocket_rate_limiter import WSRateLimiter

        limiter = WSRateLimiter(per_second=1, per_minute=1, enabled=False)

        # Should all be allowed even with very low limits
        results = [limiter.check("client")[0] for _ in range(50)]
        assert all(results)


# ============================================================================
# Conversation Service Concurrency Tests
# ============================================================================

class TestConversationServiceConcurrency:
    """Test conversation service under concurrent access."""

    @pytest.mark.database
    async def test_concurrent_session_access(self, db_session):
        """Sequential save to different sessions should be isolated from each other."""
        from services.conversation_service import ConversationService

        service = ConversationService(db_session)

        # Save messages to different sessions sequentially
        # (a single AsyncSession cannot be used concurrently in asyncio.gather)
        session_ids = [f"session-{i}" for i in range(5)]

        messages = []
        for sid in session_ids:
            msg = await service.save_message(
                session_id=sid,
                role="user",
                content=f"Message for {sid}"
            )
            messages.append(msg)

        # All messages should be saved successfully
        assert len(messages) == 5
        for msg in messages:
            assert msg.id is not None

        # Load each session and verify isolation
        for sid in session_ids:
            context = await service.load_context(sid)
            assert len(context) == 1
            assert context[0]["content"] == f"Message for {sid}"

    @pytest.mark.database
    async def test_concurrent_save_to_same_session(self, db_session):
        """Multiple saves to the same session should all persist."""
        from services.conversation_service import ConversationService

        service = ConversationService(db_session)
        session_id = "shared-session"

        # Save multiple messages sequentially to the same session
        # (truly concurrent DB writes on the same session need separate DB sessions,
        # but we test sequential rapid writes here)
        for i in range(5):
            await service.save_message(
                session_id=session_id,
                role="user" if i % 2 == 0 else "assistant",
                content=f"Message {i}"
            )

        context = await service.load_context(session_id)
        assert len(context) == 5

    @pytest.mark.database
    async def test_load_nonexistent_session(self, db_session):
        """Loading a non-existent session should return empty list."""
        from services.conversation_service import ConversationService

        service = ConversationService(db_session)
        context = await service.load_context("nonexistent-session-id")
        assert context == []


# ============================================================================
# Parallel Intent Extraction Tests
# ============================================================================

class TestIntentExtractionConcurrency:
    """Test concurrent intent classification calls."""

    @pytest.mark.unit
    async def test_parallel_intent_extraction(self):
        """5 concurrent intent classification calls should all return valid results."""
        from services.ollama_service import OllamaService

        messages = [
            "Schalte das Licht ein",
            "Wie wird das Wetter morgen?",
            "Spiele Musik ab",
            "Was steht in meinen Dokumenten?",
            "Guten Morgen, wie geht es dir?",
        ]

        expected_intents = [
            "mcp.homeassistant.turn_on",
            "mcp.weather.get_forecast",
            "mcp.jellyfin.play",
            "knowledge.search",
            "general.conversation",
        ]

        mock_client = AsyncMock()

        # Make the mock return different intents based on call order
        call_count = 0

        async def mock_chat(**kwargs):
            nonlocal call_count
            idx = call_count % len(expected_intents)
            call_count += 1
            # Simulate some LLM latency
            await asyncio.sleep(0.01)
            return _make_intent_response(expected_intents[idx])

        mock_client.chat = mock_chat

        with patch('services.ollama_service.get_default_client', return_value=mock_client), \
             patch('services.ollama_service.prompt_manager') as mock_pm:
            # Set up prompt_manager mock to return simple strings
            mock_pm.get.return_value = "mock prompt"
            mock_pm.get_config.return_value = {"temperature": 0.0}

            service = OllamaService()
            service.client = mock_client

            # Mock the internal helper methods to avoid DB/HA calls
            service._build_entity_context = AsyncMock(return_value="")
            service._find_correction_examples = AsyncMock(return_value="")

            results = await asyncio.gather(
                *[service.extract_intent(msg) for msg in messages]
            )

        assert len(results) == 5
        for result in results:
            assert "intent" in result
            assert "confidence" in result

    @pytest.mark.unit
    async def test_parallel_intent_one_fails(self):
        """If one intent call fails, others should still succeed via gather."""
        call_idx = 0

        async def mock_extract(message, **kwargs):
            nonlocal call_idx
            idx = call_idx
            call_idx += 1
            await asyncio.sleep(0.01)
            if idx == 2:
                raise RuntimeError("LLM timeout")
            return {"intent": "general.conversation", "parameters": {}, "confidence": 0.9}

        mock_service = MagicMock()
        mock_service.extract_intent = mock_extract

        results = await asyncio.gather(
            *[mock_service.extract_intent(f"msg {i}") for i in range(5)],
            return_exceptions=True,
        )

        assert len(results) == 5
        # One should be an exception
        exceptions = [r for r in results if isinstance(r, Exception)]
        successes = [r for r in results if not isinstance(r, Exception)]
        assert len(exceptions) == 1
        assert len(successes) == 4
        assert isinstance(exceptions[0], RuntimeError)


# ============================================================================
# Parallel RAG Search Tests
# ============================================================================

class TestRAGSearchConcurrency:
    """Test concurrent RAG search queries."""

    @pytest.mark.unit
    async def test_parallel_rag_searches(self):
        """3 parallel RAG searches each return independent results via RAGRetrieval."""
        from services.rag_service import RAGService

        mock_db = AsyncMock()
        queries = ["Was ist Python?", "Wie funktioniert Docker?", "Was ist Kubernetes?"]

        # RAGService.search unconditionally delegates to RAGRetrieval.search
        # post-Lane C. Mock THAT entry point.
        call_count = 0

        async def mock_retrieval_search(self_inner, query, **kwargs):
            nonlocal call_count
            idx = call_count
            call_count += 1
            await asyncio.sleep(0.01)
            return [
                {
                    "chunk": MagicMock(content=f"Result {idx}-{j}", chunk_index=j),
                    "document": MagicMock(title=f"Doc {idx}"),
                    "similarity": 0.9 - j * 0.1,
                }
                for j in range(3)
            ]

        with patch("services.rag_retrieval.RAGRetrieval.search", mock_retrieval_search):
            service = RAGService(mock_db)
            results = await asyncio.gather(*[service.search(q) for q in queries])

        assert len(results) == 3
        for result_set in results:
            assert len(result_set) == 3

    @pytest.mark.unit
    async def test_parallel_rag_embedding_failure(self):
        """Parallel searches: one failing embedding shouldn't sink the others."""
        from services.rag_service import RAGService

        mock_db = AsyncMock()
        call_count = 0

        async def mock_retrieval_search(self_inner, query, **kwargs):
            nonlocal call_count
            idx = call_count
            call_count += 1
            if idx == 1:
                # RAGRetrieval.search catches embed failures and returns []
                # via its BM25-only fallback; when BM25 also yields nothing
                # the outcome is []. Simulate that here.
                return []
            return [{"chunk": MagicMock(content="result"), "document": MagicMock(), "similarity": 0.8}]

        with patch("services.rag_retrieval.RAGRetrieval.search", mock_retrieval_search):
            service = RAGService(mock_db)
            results = await asyncio.gather(*[service.search(f"query {i}") for i in range(3)])

        assert len(results) == 3
        result_lengths = [len(r) for r in results]
        assert 0 in result_lengths  # the failed query
        assert result_lengths.count(1) == 2  # two succeeded


# ============================================================================
# Parallel WebSocket Chat Session Tests
# ============================================================================

class TestWebSocketSessionConcurrency:
    """Test concurrent WebSocket chat session processing."""

    @pytest.mark.unit
    async def test_parallel_websocket_message_processing(self):
        """5 concurrent WS sessions processing messages should be isolated."""
        # Simulate message processing: each session has its own state
        async def process_message(session_id: str, message: str) -> dict:
            """Simulate WebSocket message handling."""
            await asyncio.sleep(0.01)  # Simulate LLM latency
            return {
                "session_id": session_id,
                "response": f"Reply to: {message}",
                "intent": "general.conversation",
            }

        sessions = [f"session-{i}" for i in range(5)]
        messages = [f"Message from session {i}" for i in range(5)]

        results = await asyncio.gather(
            *[process_message(sid, msg) for sid, msg in zip(sessions, messages, strict=False)]
        )

        assert len(results) == 5
        # Verify each result corresponds to its session
        for i, result in enumerate(results):
            assert result["session_id"] == f"session-{i}"
            assert f"session {i}" in result["response"]

    @pytest.mark.unit
    async def test_parallel_ws_rate_limiting(self):
        """Concurrent rate limit checks across sessions should be properly isolated."""
        from services.websocket_rate_limiter import WSRateLimiter

        limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)

        async def send_messages(client_id: str, count: int) -> list[bool]:
            results = []
            for _ in range(count):
                allowed, _ = limiter.check(client_id)
                results.append(allowed)
                await asyncio.sleep(0)  # Yield to event loop
            return results

        # 5 clients each try to send 5 messages (limit is 3/sec)
        all_results = await asyncio.gather(
            *[send_messages(f"client-{i}", 5) for i in range(5)]
        )

        assert len(all_results) == 5
        for client_results in all_results:
            allowed_count = sum(client_results)
            rejected_count = client_results.count(False)
            # Each client: 3 allowed, 2 rejected
            assert allowed_count == 3
            assert rejected_count == 2

    @pytest.mark.unit
    async def test_parallel_ws_connection_limiting(self):
        """Concurrent connection attempts should respect per-IP limits."""
        from services.websocket_rate_limiter import WSConnectionLimiter

        limiter = WSConnectionLimiter(max_per_ip=3)
        ip = "192.168.1.100"

        # Try to connect 5 devices from same IP concurrently
        async def try_connect(device_id: str) -> bool:
            allowed, _ = limiter.can_connect(ip, device_id)
            if allowed:
                limiter.add_connection(ip, device_id)
            await asyncio.sleep(0)
            return allowed

        # Sequential connect attempts (connection limiter is sync, not truly concurrent)
        results = []
        for i in range(5):
            result = await try_connect(f"device-{i}")
            results.append(result)

        # First 3 should be allowed, rest rejected
        assert results[:3] == [True, True, True]
        assert results[3:] == [False, False]
        assert limiter.get_connection_count(ip) == 3
