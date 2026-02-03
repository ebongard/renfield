"""
Tests for AgentRouter — Unified message classification into specialized agent roles.
"""

import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch

from services.agent_router import (
    AgentRouter,
    AgentRole,
    _parse_roles,
    _filter_available_roles,
    load_roles_config,
    CONVERSATION_ROLE,
    KNOWLEDGE_ROLE,
    GENERAL_ROLE,
)


# ============================================================================
# Test fixtures
# ============================================================================

SAMPLE_CONFIG = {
    "roles": {
        "smart_home": {
            "description": {
                "de": "Smart Home: Licht, Schalter, Sensoren",
                "en": "Smart home: lights, switches, sensors",
            },
            "mcp_servers": ["homeassistant"],
            "internal_tools": ["internal.resolve_room_player", "internal.play_in_room"],
            "max_steps": 4,
            "prompt_key": "agent_prompt_smart_home",
        },
        "research": {
            "description": {
                "de": "Recherche: Websuche, Nachrichten, Wetter",
                "en": "Research: web search, news, weather",
            },
            "mcp_servers": ["search", "news", "weather"],
            "max_steps": 6,
            "prompt_key": "agent_prompt_research",
        },
        "documents": {
            "description": {
                "de": "Dokumente und E-Mail",
                "en": "Documents and email",
            },
            "mcp_servers": ["paperless", "email"],
            "max_steps": 8,
            "prompt_key": "agent_prompt_documents",
        },
        "media": {
            "description": {
                "de": "Medien: Musik, Filme, Serien",
                "en": "Media: music, movies, series",
            },
            "mcp_servers": ["jellyfin"],
            "internal_tools": ["internal.resolve_room_player", "internal.play_in_room"],
            "max_steps": 6,
            "prompt_key": "agent_prompt_media",
        },
        "workflow": {
            "description": {
                "de": "Automatisierungen: n8n Workflows",
                "en": "Automations: n8n workflows",
            },
            "mcp_servers": ["n8n"],
            "max_steps": 4,
            "prompt_key": "agent_prompt_workflow",
        },
        "knowledge": {
            "description": {
                "de": "Wissensdatenbank",
                "en": "Knowledge base",
            },
            # No agent loop
        },
        "general": {
            "description": {
                "de": "Allgemein",
                "en": "General",
            },
            "mcp_servers": None,
            "internal_tools": None,
            "max_steps": 12,
            "prompt_key": "agent_prompt",
        },
        "conversation": {
            "description": {
                "de": "Konversation",
                "en": "Conversation",
            },
            # No agent loop
        },
    }
}


def make_mock_ollama(response_text: str):
    """Create a mock OllamaService that returns a fixed response."""
    mock_ollama = MagicMock()
    mock_ollama.default_lang = "de"

    mock_response = MagicMock()
    mock_response.message.content = response_text

    mock_client = AsyncMock()
    mock_client.chat = AsyncMock(return_value=mock_response)
    mock_ollama.client = mock_client

    return mock_ollama


def make_mock_mcp_manager(connected_servers: list):
    """Create a mock MCPManager with specific connected servers."""
    mock = MagicMock()
    mock.get_connected_server_names.return_value = connected_servers
    return mock


# ============================================================================
# Test _parse_roles
# ============================================================================

class TestParseRoles:
    """Test role parsing from YAML config."""

    @pytest.mark.unit
    def test_parse_all_roles(self):
        roles = _parse_roles(SAMPLE_CONFIG)
        assert len(roles) == 8
        assert "smart_home" in roles
        assert "research" in roles
        assert "documents" in roles
        assert "media" in roles
        assert "workflow" in roles
        assert "knowledge" in roles
        assert "general" in roles
        assert "conversation" in roles

    @pytest.mark.unit
    def test_smart_home_role_properties(self):
        roles = _parse_roles(SAMPLE_CONFIG)
        role = roles["smart_home"]
        assert role.name == "smart_home"
        assert role.mcp_servers == ["homeassistant"]
        assert "internal.resolve_room_player" in role.internal_tools
        assert role.max_steps == 4
        assert role.prompt_key == "agent_prompt_smart_home"
        assert role.has_agent_loop is True

    @pytest.mark.unit
    def test_conversation_role_no_agent_loop(self):
        roles = _parse_roles(SAMPLE_CONFIG)
        role = roles["conversation"]
        assert role.has_agent_loop is False

    @pytest.mark.unit
    def test_knowledge_role_no_agent_loop(self):
        roles = _parse_roles(SAMPLE_CONFIG)
        role = roles["knowledge"]
        assert role.has_agent_loop is False

    @pytest.mark.unit
    def test_general_role_all_servers(self):
        roles = _parse_roles(SAMPLE_CONFIG)
        role = roles["general"]
        assert role.mcp_servers is None  # None = all
        assert role.internal_tools is None  # None = all
        assert role.max_steps == 12

    @pytest.mark.unit
    def test_empty_config(self):
        roles = _parse_roles({})
        assert len(roles) == 0

    @pytest.mark.unit
    def test_bilingual_descriptions(self):
        roles = _parse_roles(SAMPLE_CONFIG)
        role = roles["research"]
        assert "Recherche" in role.description["de"]
        assert "Research" in role.description["en"]


# ============================================================================
# Test _filter_available_roles
# ============================================================================

class TestFilterAvailableRoles:
    """Test role filtering based on connected MCP servers."""

    @pytest.mark.unit
    def test_no_filter_keeps_all(self):
        roles = _parse_roles(SAMPLE_CONFIG)
        filtered = _filter_available_roles(roles, connected_servers=None)
        assert len(filtered) == 8

    @pytest.mark.unit
    def test_filter_excludes_unavailable_servers(self):
        roles = _parse_roles(SAMPLE_CONFIG)
        # Only homeassistant and weather are connected
        filtered = _filter_available_roles(roles, connected_servers=["homeassistant", "weather"])
        assert "smart_home" in filtered  # homeassistant is connected
        assert "research" in filtered  # weather is connected (at least one)
        assert "documents" not in filtered  # paperless/email not connected
        assert "media" not in filtered  # jellyfin not connected
        assert "workflow" not in filtered  # n8n not connected
        # These are always kept:
        assert "general" in filtered
        assert "conversation" in filtered
        assert "knowledge" in filtered

    @pytest.mark.unit
    def test_at_least_one_server_suffices(self):
        roles = _parse_roles(SAMPLE_CONFIG)
        # research needs [search, news, weather] — having just "news" is enough
        filtered = _filter_available_roles(roles, connected_servers=["news"])
        assert "research" in filtered

    @pytest.mark.unit
    def test_empty_connected_servers(self):
        roles = _parse_roles(SAMPLE_CONFIG)
        filtered = _filter_available_roles(roles, connected_servers=[])
        # Only non-agent and null-server roles survive
        assert "general" in filtered
        assert "conversation" in filtered
        assert "knowledge" in filtered
        assert "smart_home" not in filtered


# ============================================================================
# Test AgentRouter
# ============================================================================

class TestAgentRouter:
    """Test AgentRouter initialization and role lookup."""

    @pytest.mark.unit
    def test_init_without_mcp(self):
        router = AgentRouter(SAMPLE_CONFIG)
        assert len(router.roles) == 8

    @pytest.mark.unit
    def test_init_with_mcp_filter(self):
        mcp = make_mock_mcp_manager(["homeassistant", "search"])
        router = AgentRouter(SAMPLE_CONFIG, mcp_manager=mcp)
        assert "smart_home" in router.roles
        assert "research" in router.roles
        assert "documents" not in router.roles  # paperless/email not connected

    @pytest.mark.unit
    def test_get_role_existing(self):
        router = AgentRouter(SAMPLE_CONFIG)
        role = router.get_role("smart_home")
        assert role.name == "smart_home"

    @pytest.mark.unit
    def test_get_role_fallback_to_general(self):
        router = AgentRouter(SAMPLE_CONFIG)
        role = router.get_role("nonexistent")
        assert role.name == "general"

    @pytest.mark.unit
    def test_role_descriptions_de(self):
        router = AgentRouter(SAMPLE_CONFIG)
        desc = router._build_role_descriptions("de")
        assert "smart_home" in desc
        assert "Licht" in desc

    @pytest.mark.unit
    def test_role_descriptions_en(self):
        router = AgentRouter(SAMPLE_CONFIG)
        desc = router._build_role_descriptions("en")
        assert "smart_home" in desc
        assert "lights" in desc


# ============================================================================
# Test AgentRouter.classify
# ============================================================================

class TestClassify:
    """Test LLM-based message classification."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_smart_home(self):
        """Router classifies light control as smart_home."""
        ollama = make_mock_ollama('{"role": "smart_home", "reason": "Lichtsteuerung"}')
        router = AgentRouter(SAMPLE_CONFIG)

        with patch("utils.config.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("Schalte das Licht ein", ollama)
            assert role.name == "smart_home"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_conversation(self):
        """Router classifies smalltalk as conversation."""
        ollama = make_mock_ollama('{"role": "conversation", "reason": "Smalltalk"}')
        router = AgentRouter(SAMPLE_CONFIG)

        with patch("utils.config.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("Was ist 2+2?", ollama)
            assert role.name == "conversation"
            assert role.has_agent_loop is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_documents(self):
        """Router classifies document search as documents."""
        ollama = make_mock_ollama('{"role": "documents", "reason": "Dokumentensuche"}')
        router = AgentRouter(SAMPLE_CONFIG)

        with patch("utils.config.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("Suche Rechnungen von Telekom", ollama)
            assert role.name == "documents"
            assert role.max_steps == 8

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_fallback_on_invalid_role(self):
        """Invalid role name from LLM falls back to general."""
        ollama = make_mock_ollama('{"role": "invalid_role", "reason": "test"}')
        router = AgentRouter(SAMPLE_CONFIG)

        with patch("utils.config.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("Something weird", ollama)
            assert role.name == "general"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_fallback_on_empty_response(self):
        """Empty LLM response falls back to general."""
        ollama = make_mock_ollama("")
        router = AgentRouter(SAMPLE_CONFIG)

        with patch("utils.config.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("test", ollama)
            assert role.name == "general"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_fallback_on_timeout(self):
        """Timeout falls back to general."""
        import asyncio

        ollama = MagicMock()
        ollama.default_lang = "de"
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(side_effect=asyncio.TimeoutError)
        ollama.client = mock_client

        router = AgentRouter(SAMPLE_CONFIG)

        with patch("utils.config.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("test", ollama)
            assert role.name == "general"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_fallback_on_exception(self):
        """Any exception falls back to general."""
        ollama = MagicMock()
        ollama.default_lang = "de"
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(side_effect=ConnectionError("connection lost"))
        ollama.client = mock_client

        router = AgentRouter(SAMPLE_CONFIG)

        with patch("utils.config.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("test", ollama)
            assert role.name == "general"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_with_conversation_history(self):
        """Router passes conversation history to LLM."""
        ollama = make_mock_ollama('{"role": "smart_home", "reason": "follow-up"}')
        router = AgentRouter(SAMPLE_CONFIG)

        history = [
            {"role": "user", "content": "Schalte das Licht ein"},
            {"role": "assistant", "content": "Licht eingeschaltet"},
        ]

        with patch("utils.config.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("Mach es aus", ollama, conversation_history=history)
            assert role.name == "smart_home"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_json_in_markdown(self):
        """Router can parse JSON embedded in markdown."""
        ollama = make_mock_ollama('```json\n{"role": "research", "reason": "web"}\n```')
        router = AgentRouter(SAMPLE_CONFIG)

        with patch("utils.config.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("Suche im Web", ollama)
            # The regex parser should find "research" in the text
            assert role.name == "research"


# ============================================================================
# Test _parse_classification
# ============================================================================

class TestParseClassification:
    """Test JSON parsing from LLM classification response."""

    @pytest.mark.unit
    def test_clean_json(self):
        router = AgentRouter(SAMPLE_CONFIG)
        assert router._parse_classification('{"role": "smart_home", "reason": "test"}') == "smart_home"

    @pytest.mark.unit
    def test_json_with_text(self):
        router = AgentRouter(SAMPLE_CONFIG)
        result = router._parse_classification('Here is: {"role": "documents", "reason": "test"} done.')
        assert result == "documents"

    @pytest.mark.unit
    def test_empty_response(self):
        router = AgentRouter(SAMPLE_CONFIG)
        assert router._parse_classification("") is None

    @pytest.mark.unit
    def test_plain_text_with_role_name(self):
        router = AgentRouter(SAMPLE_CONFIG)
        # Last resort: find role name in text
        result = router._parse_classification("I think this is research related")
        assert result == "research"

    @pytest.mark.unit
    def test_no_match(self):
        router = AgentRouter(SAMPLE_CONFIG)
        result = router._parse_classification("completely unrelated text xyz")
        assert result is None


# ============================================================================
# Test load_roles_config
# ============================================================================

class TestLoadRolesConfig:
    """Test YAML config loading."""

    @pytest.mark.unit
    def test_load_nonexistent_file(self):
        config = load_roles_config("/nonexistent/path.yaml")
        assert config == {}

    @pytest.mark.unit
    def test_load_actual_config(self):
        """Load the actual agent_roles.yaml from the repo."""
        config = load_roles_config("config/agent_roles.yaml")
        assert "roles" in config
        assert "smart_home" in config["roles"]
        assert "conversation" in config["roles"]


# ============================================================================
# Test Integration: Router → Filtered Tool Registry
# ============================================================================

class TestRouterToolIntegration:
    """Test that router roles correctly filter the tool registry."""

    @pytest.mark.unit
    def test_smart_home_filters_tools(self):
        """smart_home role should only get homeassistant MCP tools + internal tools."""
        from services.agent_tools import AgentToolRegistry

        roles = _parse_roles(SAMPLE_CONFIG)
        role = roles["smart_home"]

        # Create registry with filters from role
        # (Without actual MCP manager, tools will be empty but filters are applied)
        registry = AgentToolRegistry(
            server_filter=role.mcp_servers,
            internal_filter=role.internal_tools,
        )

        # With no MCP manager, only internal tools should be registered
        tool_names = registry.get_tool_names()
        for name in tool_names:
            assert name.startswith("internal."), f"Unexpected tool: {name}"

    @pytest.mark.unit
    def test_general_role_no_filter(self):
        """general role should pass None filters (= all tools)."""
        roles = _parse_roles(SAMPLE_CONFIG)
        role = roles["general"]
        assert role.mcp_servers is None
        assert role.internal_tools is None

    @pytest.mark.unit
    def test_agent_service_with_role(self):
        """AgentService accepts role parameter and uses its max_steps."""
        from services.agent_service import AgentService
        from services.agent_tools import AgentToolRegistry

        roles = _parse_roles(SAMPLE_CONFIG)
        role = roles["smart_home"]

        registry = AgentToolRegistry()
        agent = AgentService(registry, role=role)

        assert agent.max_steps == 4
        assert agent._prompt_key == "agent_prompt_smart_home"

    @pytest.mark.unit
    def test_agent_service_without_role(self):
        """AgentService without role uses settings defaults."""
        from services.agent_service import AgentService
        from services.agent_tools import AgentToolRegistry

        registry = AgentToolRegistry()
        agent = AgentService(registry)

        # Should use settings.agent_max_steps (12 by default)
        assert agent._prompt_key == "agent_prompt"


# ============================================================================
# Test Pre-built Roles
# ============================================================================

class TestPrebuiltRoles:
    """Test pre-built fallback role constants."""

    @pytest.mark.unit
    def test_conversation_role(self):
        assert CONVERSATION_ROLE.name == "conversation"
        assert CONVERSATION_ROLE.has_agent_loop is False

    @pytest.mark.unit
    def test_knowledge_role(self):
        assert KNOWLEDGE_ROLE.name == "knowledge"
        assert KNOWLEDGE_ROLE.has_agent_loop is False

    @pytest.mark.unit
    def test_general_role(self):
        assert GENERAL_ROLE.name == "general"
        assert GENERAL_ROLE.mcp_servers is None
        assert GENERAL_ROLE.max_steps == 12
