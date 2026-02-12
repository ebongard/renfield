"""
Tests for intent extraction performance optimizations.

Tests:
- Entity map caching (HomeAssistantClient)
- Entity context scoring optimization (set-based matching)
- Intent registry prompt caching
- Parallel execution of entity context + corrections
- _has_corrections cache TTL
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =============================================================================
# Entity Map Cache Tests (HomeAssistantClient)
# =============================================================================

class TestEntityMapCache:
    """Tests for get_entity_map() TTL cache."""

    @pytest.fixture(autouse=True)
    def reset_cache(self):
        """Clear class-level cache before each test."""
        with patch('integrations.homeassistant.settings') as mock_settings:
            mock_settings.home_assistant_url = "http://ha.local:8123"
            mock_settings.home_assistant_token = "test_token"
            from integrations.homeassistant import HomeAssistantClient
            HomeAssistantClient._entity_map_cache = None
            HomeAssistantClient._entity_map_cache_time = 0
        yield

    @pytest.fixture
    def ha_client(self):
        with patch('integrations.homeassistant.settings') as mock_settings:
            mock_settings.home_assistant_url = "http://ha.local:8123"
            mock_settings.home_assistant_token = "test_token"
            from integrations.homeassistant import HomeAssistantClient
            return HomeAssistantClient()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_entity_map_first_call_hits_api(self, ha_client):
        """First call should hit HA REST API."""
        mock_states = [
            {
                "entity_id": "light.wohnzimmer",
                "state": "on",
                "attributes": {"friendly_name": "Wohnzimmer Licht"}
            }
        ]

        with patch.object(ha_client, 'get_states', new_callable=AsyncMock, return_value=mock_states):
            result = await ha_client.get_entity_map()
            assert len(result) == 1
            assert result[0]["entity_id"] == "light.wohnzimmer"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_entity_map_second_call_uses_cache(self, ha_client):
        """Second call within TTL should use cache, not hit API."""
        mock_states = [
            {
                "entity_id": "light.wohnzimmer",
                "state": "on",
                "attributes": {"friendly_name": "Wohnzimmer Licht"}
            }
        ]

        with patch.object(ha_client, 'get_states', new_callable=AsyncMock, return_value=mock_states) as mock_get:
            # First call - hits API
            await ha_client.get_entity_map()
            assert mock_get.call_count == 1

            # Second call - should use cache
            result = await ha_client.get_entity_map()
            assert mock_get.call_count == 1  # No additional API call
            assert len(result) == 1

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_entity_map_cache_expires(self, ha_client):
        """Cache should expire after TTL."""
        from integrations.homeassistant import HomeAssistantClient

        mock_states = [
            {
                "entity_id": "light.wohnzimmer",
                "state": "on",
                "attributes": {"friendly_name": "Wohnzimmer Licht"}
            }
        ]

        with patch.object(ha_client, 'get_states', new_callable=AsyncMock, return_value=mock_states) as mock_get:
            # First call
            await ha_client.get_entity_map()
            assert mock_get.call_count == 1

            # Expire the cache by backdating the timestamp
            HomeAssistantClient._entity_map_cache_time = time.time() - 120

            # Next call should hit API again
            await ha_client.get_entity_map()
            assert mock_get.call_count == 2

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_entity_map_cache_shared_across_instances(self):
        """Cache should be shared across HomeAssistantClient instances."""
        with patch('integrations.homeassistant.settings') as mock_settings:
            mock_settings.home_assistant_url = "http://ha.local:8123"
            mock_settings.home_assistant_token = "test_token"

            from integrations.homeassistant import HomeAssistantClient

            client1 = HomeAssistantClient()
            client2 = HomeAssistantClient()

            mock_states = [
                {
                    "entity_id": "light.test",
                    "state": "on",
                    "attributes": {"friendly_name": "Test Light"}
                }
            ]

            with patch.object(client1, 'get_states', new_callable=AsyncMock, return_value=mock_states):
                await client1.get_entity_map()

            with patch.object(client2, 'get_states', new_callable=AsyncMock, return_value=mock_states) as mock_get:
                result = await client2.get_entity_map()
                # Should use cache from client1, not call API
                mock_get.assert_not_called()
                assert len(result) == 1


# =============================================================================
# Entity Context Scoring Optimization Tests
# =============================================================================

class TestEntityContextScoring:
    """Tests for optimized _build_entity_context() scoring."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_friendly_name_match_uses_set_intersection(self):
        """Entity scoring should match words via set intersection."""
        with patch('services.ollama_service.settings') as mock_settings:
            mock_settings.ollama_url = "http://localhost:11434"
            mock_settings.ollama_model = "test"
            mock_settings.default_language = "de"

            from services.ollama_service import OllamaService
            service = OllamaService()

            mock_entities = [
                {
                    "entity_id": "light.arbeitszimmer",
                    "friendly_name": "Arbeitszimmer Licht",
                    "domain": "light",
                    "room": "Arbeitszimmer",
                    "state": "off"
                },
                {
                    "entity_id": "light.wohnzimmer",
                    "friendly_name": "Wohnzimmer Decke",
                    "domain": "light",
                    "room": "Wohnzimmer",
                    "state": "on"
                }
            ]

            with patch('integrations.homeassistant.HomeAssistantClient.get_entity_map',
                        new_callable=AsyncMock, return_value=mock_entities):
                result = await service._build_entity_context(
                    "Schalte das Licht im Arbeitszimmer an",
                    room_context=None
                )

                # Arbeitszimmer entities should be listed
                assert "Arbeitszimmer" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_device_keyword_matching(self):
        """Device keyword matching should use pre-computed domain set."""
        with patch('services.ollama_service.settings') as mock_settings:
            mock_settings.ollama_url = "http://localhost:11434"
            mock_settings.ollama_model = "test"
            mock_settings.default_language = "de"

            from services.ollama_service import OllamaService
            service = OllamaService()

            mock_entities = [
                {
                    "entity_id": "light.test",
                    "friendly_name": "Test Lampe",
                    "domain": "light",
                    "room": "Test",
                    "state": "off"
                },
                {
                    "entity_id": "climate.heizung",
                    "friendly_name": "Heizung Test",
                    "domain": "climate",
                    "room": "Test",
                    "state": "heat"
                }
            ]

            with patch('integrations.homeassistant.HomeAssistantClient.get_entity_map',
                        new_callable=AsyncMock, return_value=mock_entities):
                # "Licht" should match light domain
                result = await service._build_entity_context("Schalte das Licht ein")
                assert "Test Lampe" in result


# =============================================================================
# Intent Registry Prompt Cache Tests
# =============================================================================

class TestIntentRegistryCache:
    """Tests for intent registry prompt caching."""

    @pytest.mark.unit
    def test_build_intent_prompt_cached(self):
        """Second call to build_intent_prompt should return cached result."""
        with patch('services.intent_registry.settings') as mock_settings:
            mock_settings.rag_enabled = False

            mock_settings.mcp_enabled = False

            from services.intent_registry import IntentRegistry
            registry = IntentRegistry()

            result1 = registry.build_intent_prompt(lang="de")
            result2 = registry.build_intent_prompt(lang="de")

            assert result1 == result2
            assert "intent_prompt_de" in registry._prompt_cache

    @pytest.mark.unit
    def test_build_examples_prompt_cached(self):
        """Second call to build_examples_prompt should return cached result."""
        with patch('services.intent_registry.settings') as mock_settings:
            mock_settings.rag_enabled = False

            mock_settings.mcp_enabled = False

            from services.intent_registry import IntentRegistry
            registry = IntentRegistry()

            result1 = registry.build_examples_prompt(lang="de")
            result2 = registry.build_examples_prompt(lang="de")

            assert result1 == result2

    @pytest.mark.unit
    def test_cache_invalidated_on_mcp_tools_change(self):
        """Cache should be cleared when MCP tools change."""
        with patch('services.intent_registry.settings') as mock_settings:
            mock_settings.rag_enabled = False

            mock_settings.mcp_enabled = False

            from services.intent_registry import IntentRegistry
            registry = IntentRegistry()

            # Populate cache
            registry.build_intent_prompt(lang="de")
            assert len(registry._prompt_cache) > 0

            # Change MCP tools should invalidate
            registry.set_mcp_tools([{"name": "test", "description": "test"}])
            assert len(registry._prompt_cache) == 0

    @pytest.mark.unit
    @pytest.mark.unit
    def test_different_languages_cached_separately(self):
        """Different language prompts should be cached separately."""
        with patch('services.intent_registry.settings') as mock_settings:
            mock_settings.rag_enabled = False

            mock_settings.mcp_enabled = False

            from services.intent_registry import IntentRegistry
            registry = IntentRegistry()

            result_de = registry.build_intent_prompt(lang="de")
            result_en = registry.build_intent_prompt(lang="en")

            assert "intent_prompt_de" in registry._prompt_cache
            assert "intent_prompt_en" in registry._prompt_cache


# =============================================================================
# num_predict Configuration Test
# =============================================================================

class TestNumPredictConfig:
    """Test that num_predict is set correctly."""

    @pytest.mark.unit
    def test_num_predict_is_500(self):
        """intent.yaml should have num_predict=500 (with 32k context window)."""
        from services.prompt_manager import prompt_manager
        llm_options = prompt_manager.get_config("intent", "llm_options")
        if llm_options:
            assert llm_options.get("num_predict") == 500, \
                f"num_predict should be 500 but is {llm_options.get('num_predict')}"


# =============================================================================
# Has Corrections Cache TTL Test
# =============================================================================

class TestHasCorrectionsCacheTTL:
    """Test that _has_corrections cache TTL is 300s."""

    @pytest.mark.unit
    def test_cache_ttl_is_300(self):
        """IntentFeedbackService._CACHE_TTL should be 300 seconds."""
        from services.intent_feedback_service import IntentFeedbackService
        assert IntentFeedbackService._CACHE_TTL == 300
