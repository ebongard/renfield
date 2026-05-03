"""
Tests for AgentRouter — Unified message classification into specialized agent roles.
"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure 'ollama' module is available even when the package isn't installed.
# get_agent_client() does `import ollama` internally, so we provide a stub.
if "ollama" not in sys.modules:
    _ollama_stub = MagicMock()
    _ollama_stub.AsyncClient = MagicMock()
    sys.modules["ollama"] = _ollama_stub

from services.agent_router import (
    CONVERSATION_ROLE,
    GENERAL_ROLE,
    KNOWLEDGE_ROLE,
    AgentRouter,
    _filter_available_roles,
    _parse_roles,
    load_roles_config,
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
        "routine": {
            "description": {
                "de": "Routinen: Gute-Nacht-Routine, Guten-Morgen-Routine",
                "en": "Routines: good-night routine, good-morning routine",
            },
            "mcp_servers": ["homeassistant", "calendar", "weather", "jellyfin", "dlna", "radio"],
            "internal_tools": [
                "internal.get_all_presence",
                "internal.get_user_location",
                "internal.resolve_room_player",
                "internal.media_control",
                "internal.play_in_room",
                "internal.play_radio",
            ],
            "max_steps": 15,
            "prompt_key": "agent_prompt_routine",
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
        "presence": {
            "description": {
                "de": "Anwesenheit: Wo ist eine Person, wer ist zuhause",
                "en": "Presence: where is a person, who is home",
            },
            "mcp_servers": [],
            "internal_tools": ["internal.get_user_location", "internal.get_all_presence"],
            "max_steps": 2,
            "prompt_key": "agent_prompt",
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

# Config with sub_intents for testing
SAMPLE_CONFIG_WITH_SUB_INTENTS = {
    "roles": {
        **SAMPLE_CONFIG["roles"],
        "release": {
            "description": {
                "de": "Release-Management",
                "en": "Release management",
            },
            "mcp_servers": ["release"],
            "max_steps": 10,
            "prompt_key": "agent_prompt",
            "sub_intents": {
                "my_dashboard": {
                    "de": "Mein Dashboard, meine Releases",
                    "en": "My dashboard, my releases",
                },
            },
        },
        "memory_role": {
            "description": {
                "de": "Erinnerungen",
                "en": "Memory",
            },
            "prompt_key": "agent_prompt",
            "sub_intents": {
                "delete": {
                    "de": "Erinnerungen loeschen",
                    "en": "Delete memories",
                },
                "list": {
                    "de": "Erinnerungen anzeigen",
                    "en": "Show memories",
                },
            },
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
        assert len(roles) == 10
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
    def test_presence_role_properties(self):
        roles = _parse_roles(SAMPLE_CONFIG)
        role = roles["presence"]
        assert role.name == "presence"
        assert role.mcp_servers == []
        assert role.internal_tools == ["internal.get_user_location", "internal.get_all_presence"]
        assert role.max_steps == 2
        assert role.has_agent_loop is True

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

    @pytest.mark.unit
    def test_per_role_model_override(self):
        """Test that per-role model and ollama_url are parsed."""
        config = {
            "roles": {
                "smart_home": {
                    "description": {"de": "Smart Home", "en": "Smart home"},
                    "mcp_servers": ["homeassistant"],
                    "max_steps": 4,
                    "prompt_key": "agent_prompt_smart_home",
                    "model": "llama3.2:3b",
                    "ollama_url": "http://ollama:11434",
                },
                "general": {
                    "description": {"de": "Allgemein", "en": "General"},
                    "max_steps": 12,
                    "prompt_key": "agent_prompt",
                    # No model/ollama_url → should be None
                },
            }
        }
        roles = _parse_roles(config)
        assert roles["smart_home"].model == "llama3.2:3b"
        assert roles["smart_home"].ollama_url == "http://ollama:11434"
        assert roles["general"].model is None
        assert roles["general"].ollama_url is None

    @pytest.mark.unit
    def test_native_function_calling_default_false(self):
        """A role that does not set the flag gets native_function_calling=False."""
        roles = _parse_roles(SAMPLE_CONFIG)
        for role in roles.values():
            assert role.native_function_calling is False

    @pytest.mark.unit
    def test_native_function_calling_opt_in(self):
        """Setting native_function_calling: true in YAML is parsed."""
        config = {
            "roles": {
                "experimental": {
                    "description": {"de": "x", "en": "x"},
                    "mcp_servers": [],
                    "prompt_key": "agent_prompt",
                    "native_function_calling": True,
                },
                "normal": {
                    "description": {"de": "y", "en": "y"},
                    "mcp_servers": [],
                    "prompt_key": "agent_prompt",
                },
            }
        }
        roles = _parse_roles(config)
        assert roles["experimental"].native_function_calling is True
        assert roles["normal"].native_function_calling is False

    @pytest.mark.unit
    def test_native_function_calling_accepts_truthy_values(self):
        """Truthy strings / 1 coerce to True, falsy to False (YAML may parse loosely)."""
        config = {
            "roles": {
                "a": {"description": {"de": "a", "en": "a"}, "mcp_servers": [],
                      "prompt_key": "agent_prompt", "native_function_calling": 1},
                "b": {"description": {"de": "b", "en": "b"}, "mcp_servers": [],
                      "prompt_key": "agent_prompt", "native_function_calling": 0},
                "c": {"description": {"de": "c", "en": "c"}, "mcp_servers": [],
                      "prompt_key": "agent_prompt", "native_function_calling": None},
            }
        }
        roles = _parse_roles(config)
        assert roles["a"].native_function_calling is True
        assert roles["b"].native_function_calling is False
        assert roles["c"].native_function_calling is False


# ============================================================================
# Test _filter_available_roles
# ============================================================================

class TestFilterAvailableRoles:
    """Test role filtering based on connected MCP servers."""

    @pytest.mark.unit
    def test_no_filter_keeps_all(self):
        roles = _parse_roles(SAMPLE_CONFIG)
        filtered = _filter_available_roles(roles, connected_servers=None)
        assert len(filtered) == 10

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

    @pytest.mark.unit
    def test_internal_only_role_always_available(self):
        """Roles with mcp_servers=[] (internal-only) are always kept."""
        roles = _parse_roles(SAMPLE_CONFIG)
        # Even with no connected servers, presence should be available
        filtered = _filter_available_roles(roles, connected_servers=[])
        assert "presence" in filtered
        assert filtered["presence"].has_agent_loop is True


# ============================================================================
# Test AgentRouter
# ============================================================================

class TestAgentRouter:
    """Test AgentRouter initialization and role lookup."""

    @pytest.mark.unit
    def test_init_without_mcp(self):
        router = AgentRouter(SAMPLE_CONFIG)
        assert len(router.roles) == 10

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

        with patch("services.agent_router.settings") as mock_settings:
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

        with patch("services.agent_router.settings") as mock_settings:
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

        with patch("services.agent_router.settings") as mock_settings:
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

        with patch("services.agent_router.settings") as mock_settings:
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

        with patch("services.agent_router.settings") as mock_settings:
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

        with patch("services.agent_router.settings") as mock_settings:
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

        with patch("services.agent_router.settings") as mock_settings:
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

        with patch("services.agent_router.settings") as mock_settings:
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

        with patch("services.agent_router.settings") as mock_settings:
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
        role, sub = router._parse_classification('{"role": "smart_home", "reason": "test"}')
        assert role == "smart_home"
        assert sub is None

    @pytest.mark.unit
    def test_json_with_text(self):
        router = AgentRouter(SAMPLE_CONFIG)
        role, sub = router._parse_classification('Here is: {"role": "documents", "reason": "test"} done.')
        assert role == "documents"
        assert sub is None

    @pytest.mark.unit
    def test_empty_response(self):
        router = AgentRouter(SAMPLE_CONFIG)
        role, sub = router._parse_classification("")
        assert role is None
        assert sub is None

    @pytest.mark.unit
    def test_plain_text_with_role_name(self):
        router = AgentRouter(SAMPLE_CONFIG)
        # Last resort: find role name in text
        role, sub = router._parse_classification("I think this is research related")
        assert role == "research"
        assert sub is None

    @pytest.mark.unit
    def test_no_match(self):
        router = AgentRouter(SAMPLE_CONFIG)
        role, sub = router._parse_classification("completely unrelated text xyz")
        assert role is None
        assert sub is None

    @pytest.mark.unit
    def test_parse_classification_with_sub_intent(self):
        """JSON with sub_intent returns both role and sub_intent."""
        router = AgentRouter(SAMPLE_CONFIG)
        role, sub = router._parse_classification('{"role": "smart_home", "sub_intent": "my_dashboard", "reason": "test"}')
        assert role == "smart_home"
        assert sub == "my_dashboard"

    @pytest.mark.unit
    def test_parse_classification_without_sub_intent_key(self):
        """JSON without sub_intent key returns None for sub_intent (backward compat)."""
        router = AgentRouter(SAMPLE_CONFIG)
        role, sub = router._parse_classification('{"role": "research", "reason": "web search"}')
        assert role == "research"
        assert sub is None

    @pytest.mark.unit
    def test_parse_classification_sub_intent_in_text(self):
        """JSON-in-text extraction includes sub_intent."""
        router = AgentRouter(SAMPLE_CONFIG)
        role, sub = router._parse_classification(
            'Here: {"role": "smart_home", "sub_intent": "test_sub", "reason": "x"} end.'
        )
        assert role == "smart_home"
        assert sub == "test_sub"

    @pytest.mark.unit
    def test_parse_classification_null_sub_intent(self):
        """JSON with sub_intent: null returns None."""
        router = AgentRouter(SAMPLE_CONFIG)
        role, sub = router._parse_classification('{"role": "research", "sub_intent": null, "reason": "x"}')
        assert role == "research"
        assert sub is None


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
            _init_only=True,
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

        registry = AgentToolRegistry(_init_only=True)
        agent = AgentService(registry, role=role)

        assert agent.max_steps == 4
        assert agent._prompt_key == "agent_prompt_smart_home"

    @pytest.mark.unit
    def test_presence_role_only_gets_presence_tools(self):
        """presence role should only get presence internal tools, no MCP tools."""
        from services.agent_tools import AgentToolRegistry

        roles = _parse_roles(SAMPLE_CONFIG)
        role = roles["presence"]

        registry = AgentToolRegistry(
            server_filter=role.mcp_servers,
            internal_filter=role.internal_tools,
            _init_only=True,
        )

        tool_names = registry.get_tool_names()
        assert "internal.get_user_location" in tool_names
        assert "internal.get_all_presence" in tool_names
        # No other tools should be present
        for name in tool_names:
            assert name in ("internal.get_user_location", "internal.get_all_presence"), f"Unexpected tool: {name}"

    @pytest.mark.unit
    def test_agent_service_without_role(self):
        """AgentService without role uses settings defaults."""
        from services.agent_service import AgentService
        from services.agent_tools import AgentToolRegistry

        registry = AgentToolRegistry(_init_only=True)
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


# ============================================================================
# Test Routine Role
# ============================================================================

class TestRoutineRole:
    """Test the routine role for good-night / good-morning sequences."""

    @pytest.mark.unit
    def test_routine_role_properties(self):
        roles = _parse_roles(SAMPLE_CONFIG)
        role = roles["routine"]
        assert role.name == "routine"
        assert role.max_steps == 15
        assert role.prompt_key == "agent_prompt_routine"
        assert role.has_agent_loop is True
        assert "homeassistant" in role.mcp_servers
        assert "calendar" in role.mcp_servers
        assert "weather" in role.mcp_servers
        assert "internal.get_all_presence" in role.internal_tools
        assert "internal.media_control" in role.internal_tools

    @pytest.mark.unit
    def test_routine_role_available_with_homeassistant(self):
        """Routine role is available when at least one of its MCP servers is connected."""
        roles = _parse_roles(SAMPLE_CONFIG)
        filtered = _filter_available_roles(roles, connected_servers=["homeassistant"])
        assert "routine" in filtered

    @pytest.mark.unit
    def test_routine_role_unavailable_without_servers(self):
        """Routine role is excluded when none of its MCP servers are connected."""
        roles = _parse_roles(SAMPLE_CONFIG)
        filtered = _filter_available_roles(roles, connected_servers=[])
        assert "routine" not in filtered

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_gute_nacht(self):
        """'Gute Nacht' is classified as routine."""
        ollama = make_mock_ollama('{"role": "routine", "reason": "Gute-Nacht-Routine"}')
        router = AgentRouter(SAMPLE_CONFIG)

        with patch("services.agent_router.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("Gute Nacht", ollama)
            assert role.name == "routine"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_guten_morgen(self):
        """'Guten Morgen' is classified as routine."""
        ollama = make_mock_ollama('{"role": "routine", "reason": "Guten-Morgen-Routine"}')
        router = AgentRouter(SAMPLE_CONFIG)

        with patch("services.agent_router.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("Guten Morgen", ollama)
            assert role.name == "routine"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_ich_gehe_schlafen(self):
        """'Ich gehe schlafen' is classified as routine."""
        ollama = make_mock_ollama('{"role": "routine", "reason": "Schlafenszeit"}')
        router = AgentRouter(SAMPLE_CONFIG)

        with patch("services.agent_router.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("Ich gehe schlafen", ollama)
            assert role.name == "routine"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_casual_morgen_as_conversation(self):
        """Casual 'Morgen!' is classified as conversation, not routine."""
        ollama = make_mock_ollama('{"role": "conversation", "reason": "Beilaeufiger Gruss"}')
        router = AgentRouter(SAMPLE_CONFIG)

        with patch("services.agent_router.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("Morgen!", ollama)
            assert role.name == "conversation"

    @pytest.mark.unit
    def test_routine_in_actual_config(self):
        """Verify that the actual agent_roles.yaml contains the routine role."""
        config = load_roles_config("config/agent_roles.yaml")
        assert "routine" in config["roles"]
        role_cfg = config["roles"]["routine"]
        assert "homeassistant" in role_cfg["mcp_servers"]
        assert role_cfg["max_steps"] == 15
        assert role_cfg["prompt_key"] == "agent_prompt_routine"


# ============================================================================
# Test Sub-Intent Parsing & Classification
# ============================================================================

class TestSubIntentParsing:
    """Test sub_intent parsing from config and classification."""

    @pytest.mark.unit
    def test_parse_roles_with_sub_intents(self):
        """Roles with sub_intents config get sub_intent_definitions populated."""
        roles = _parse_roles(SAMPLE_CONFIG_WITH_SUB_INTENTS)
        role = roles["release"]
        assert role.sub_intent_definitions is not None
        assert "my_dashboard" in role.sub_intent_definitions
        assert role.sub_intent_definitions["my_dashboard"]["de"] == "Mein Dashboard, meine Releases"
        assert role.sub_intent is None  # Not set on shared role

    @pytest.mark.unit
    def test_parse_roles_without_sub_intents(self):
        """Roles without sub_intents config have sub_intent_definitions=None."""
        roles = _parse_roles(SAMPLE_CONFIG)
        role = roles["smart_home"]
        assert role.sub_intent_definitions is None
        assert role.sub_intent is None

    @pytest.mark.unit
    def test_parse_roles_string_sub_intent(self):
        """String sub_intent values are normalized to {de: str, en: str}."""
        config = {
            "roles": {
                "test_role": {
                    "description": {"de": "Test", "en": "Test"},
                    "prompt_key": "agent_prompt",
                    "sub_intents": {
                        "simple": "A simple sub-intent",
                    },
                },
            }
        }
        roles = _parse_roles(config)
        role = roles["test_role"]
        assert role.sub_intent_definitions["simple"] == {"de": "A simple sub-intent", "en": "A simple sub-intent"}

    @pytest.mark.unit
    def test_parse_roles_multiple_sub_intents(self):
        """Role with multiple sub_intents gets all of them."""
        roles = _parse_roles(SAMPLE_CONFIG_WITH_SUB_INTENTS)
        role = roles["memory_role"]
        assert role.sub_intent_definitions is not None
        assert "delete" in role.sub_intent_definitions
        assert "list" in role.sub_intent_definitions

    @pytest.mark.unit
    def test_build_role_descriptions_includes_sub_intents(self):
        """Role descriptions include sub_intent lines with > prefix."""
        router = AgentRouter(SAMPLE_CONFIG_WITH_SUB_INTENTS)
        desc = router._build_role_descriptions("de")
        assert "> release/my_dashboard:" in desc
        assert "Mein Dashboard" in desc
        # Memory role sub_intents
        assert "> memory_role/delete:" in desc
        assert "> memory_role/list:" in desc

    @pytest.mark.unit
    def test_build_role_descriptions_en(self):
        """Sub_intent descriptions use the correct language."""
        router = AgentRouter(SAMPLE_CONFIG_WITH_SUB_INTENTS)
        desc = router._build_role_descriptions("en")
        assert "My dashboard" in desc
        assert "Delete memories" in desc

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_valid_sub_intent(self):
        """Valid sub_intent is set on the returned role copy."""
        ollama = make_mock_ollama('{"role": "release", "sub_intent": "my_dashboard", "reason": "dashboard"}')
        router = AgentRouter(SAMPLE_CONFIG_WITH_SUB_INTENTS)

        with patch("services.agent_router.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("Meine Releases", ollama)
            assert role.name == "release"
            assert role.sub_intent == "my_dashboard"
            # Shared role in router.roles must NOT be mutated
            assert router.roles["release"].sub_intent is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_sub_intent_with_role_prefix(self):
        """LLM returns 'release/my_dashboard' — role prefix is stripped."""
        ollama = make_mock_ollama('{"role": "release", "sub_intent": "release/my_dashboard", "reason": "dashboard"}')
        router = AgentRouter(SAMPLE_CONFIG_WITH_SUB_INTENTS)

        with patch("services.agent_router.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("Meine Releases", ollama)
            assert role.name == "release"
            assert role.sub_intent == "my_dashboard"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_invalid_sub_intent(self):
        """Undefined sub_intent is discarded (set to None)."""
        ollama = make_mock_ollama('{"role": "release", "sub_intent": "nonexistent", "reason": "test"}')
        router = AgentRouter(SAMPLE_CONFIG_WITH_SUB_INTENTS)

        with patch("services.agent_router.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("Something", ollama)
            assert role.name == "release"
            assert role.sub_intent is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_sub_intent_no_definitions(self):
        """Role without sub_intent_definitions always returns sub_intent=None."""
        ollama = make_mock_ollama('{"role": "smart_home", "sub_intent": "anything", "reason": "test"}')
        router = AgentRouter(SAMPLE_CONFIG_WITH_SUB_INTENTS)

        with patch("services.agent_router.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("Licht ein", ollama)
            assert role.name == "smart_home"
            assert role.sub_intent is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_classify_no_sub_intent_from_llm(self):
        """When LLM returns no sub_intent, role.sub_intent is None."""
        ollama = make_mock_ollama('{"role": "release", "reason": "general release query"}')
        router = AgentRouter(SAMPLE_CONFIG_WITH_SUB_INTENTS)

        with patch("services.agent_router.settings") as mock_settings:
            mock_settings.ollama_intent_model = "test-model"
            mock_settings.ollama_model = "test-model"
            mock_settings.agent_ollama_url = None

            role = await router.classify("Zeige alle Releases", ollama)
            assert role.name == "release"
            assert role.sub_intent is None


# ============================================================================
# Dedicated router model/URL settings
# ============================================================================

class TestRouterModelSettings:
    """Tests for agent_router_model and agent_router_url config fields."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_router_uses_dedicated_model(self):
        """agent_router_model takes priority over ollama_intent_model."""
        router = AgentRouter(SAMPLE_CONFIG)
        ollama = make_mock_ollama('{"role": "conversation"}')

        with patch("services.agent_router.settings") as s, \
             patch("services.agent_router.get_agent_client") as gac:
            s.agent_router_model = "qwen3:1.7b"
            s.agent_router_url = None
            s.agent_ollama_url = None
            s.ollama_intent_model = "qwen3:8b"
            s.ollama_model = "llama3.2:3b"

            await router.classify("Hallo", ollama)
            # Should NOT use get_agent_client (no router_url set)
            gac.assert_not_called()
            # Model used should be agent_router_model
            call_kwargs = ollama.client.chat.call_args
            assert call_kwargs.kwargs["model"] == "qwen3:1.7b"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_router_falls_back_to_intent_model(self):
        """Without agent_router_model, falls back to ollama_intent_model."""
        router = AgentRouter(SAMPLE_CONFIG)
        ollama = make_mock_ollama('{"role": "conversation"}')

        with patch("services.agent_router.settings") as s:
            s.agent_router_model = None
            s.agent_router_url = None
            s.agent_ollama_url = None
            s.ollama_intent_model = "qwen3:8b"
            s.ollama_model = "llama3.2:3b"

            await router.classify("Hallo", ollama)
            call_kwargs = ollama.client.chat.call_args
            assert call_kwargs.kwargs["model"] == "qwen3:8b"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_router_uses_dedicated_url(self):
        """agent_router_url routes to a separate Ollama instance."""
        router = AgentRouter(SAMPLE_CONFIG)
        mock_response = MagicMock()
        mock_response.message.content = '{"role": "conversation"}'
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value=mock_response)

        ollama = make_mock_ollama('{"role": "conversation"}')

        with patch("services.agent_router.settings") as s, \
             patch("services.agent_router.get_agent_client", return_value=(mock_client, None)) as gac:
            s.agent_router_model = "qwen3:1.7b"
            s.agent_router_url = "http://fast-gpu:11434"
            s.agent_ollama_url = "http://main-gpu:11434"
            s.ollama_intent_model = "qwen3:8b"
            s.ollama_model = "llama3.2:3b"

            await router.classify("Hallo", ollama)
            gac.assert_called_once_with(fallback_url="http://fast-gpu:11434")
            call_kwargs = mock_client.chat.call_args
            assert call_kwargs.kwargs["model"] == "qwen3:1.7b"


# ============================================================================
# Test AgentRole.utterances + SemanticRouter integration
# ============================================================================


class TestAgentRoleUtterances:
    """Test utterances field on AgentRole."""

    @pytest.mark.unit
    def test_utterances_default_none(self):
        from services.agent_router import AgentRole
        role = AgentRole(name="test", description={"de": "Test"})
        assert role.utterances is None

    @pytest.mark.unit
    def test_utterances_from_list(self):
        from services.agent_router import AgentRole
        role = AgentRole(
            name="test",
            description={"de": "Test"},
            utterances=["hello", "world"],
        )
        assert role.utterances == ["hello", "world"]

    @pytest.mark.unit
    def test_utterances_parsed_from_config(self):
        from services.agent_router import _parse_roles
        config = {
            "roles": {
                "smart_home": {
                    "description": {"de": "Smart Home"},
                    "prompt_key": "agent_prompt_smart_home",
                    "utterances": ["Licht an", "Temperatur"],
                }
            }
        }
        roles = _parse_roles(config)
        assert roles["smart_home"].utterances == ["Licht an", "Temperatur"]

    @pytest.mark.unit
    def test_utterances_none_when_not_in_config(self):
        from services.agent_router import _parse_roles
        config = {
            "roles": {
                "general": {
                    "description": {"de": "General"},
                    "prompt_key": "agent_prompt",
                }
            }
        }
        roles = _parse_roles(config)
        assert roles["general"].utterances is None


class TestSetSemanticRouter:
    """Test semantic router wiring on AgentRouter."""

    @pytest.mark.unit
    def test_set_semantic_router(self):
        from services.agent_router import AgentRouter
        router = AgentRouter({"roles": {}})
        assert router._semantic_router is None

        mock_sr = MagicMock()
        router.set_semantic_router(mock_sr)
        assert router._semantic_router is mock_sr

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_semantic_fast_path_used(self):
        """When semantic router matches, LLM should not be called."""
        from services.agent_router import AgentRouter
        config = {
            "roles": {
                "smart_home": {
                    "description": {"de": "Smart Home"},
                    "prompt_key": "agent_prompt_smart_home",
                }
            }
        }
        router = AgentRouter(config)

        mock_sr = AsyncMock()
        mock_sr.classify.return_value = ("smart_home", None, 0.92)
        router.set_semantic_router(mock_sr)

        ollama = MagicMock()
        role = await router.classify("Licht an", ollama)
        assert role.name == "smart_home"
        mock_sr.classify.assert_called_once_with("Licht an")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_semantic_fallback_to_llm(self):
        """When semantic router returns None, LLM classification path is attempted."""
        from services.agent_router import AgentRouter

        config = {
            "roles": {
                "general": {
                    "description": {"de": "Allgemein"},
                    "prompt_key": "agent_prompt",
                }
            }
        }
        router = AgentRouter(config)

        mock_sr = AsyncMock()
        mock_sr.classify.return_value = (None, None, 0.3)
        router.set_semantic_router(mock_sr)

        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.message.content = '{"role": "general"}'
        mock_client.chat.return_value = mock_response

        ollama = MagicMock()

        with patch("services.agent_router.settings") as s, \
             patch("services.agent_router.get_agent_client", return_value=(mock_client, None)):
            s.agent_router_model = ""
            s.agent_router_url = ""
            s.agent_ollama_url = ""
            s.ollama_intent_model = ""
            s.ollama_model = "llama3.2:3b"
            role = await router.classify("something", ollama)
            # Semantic router was called and returned None
            mock_sr.classify.assert_called_once_with("something")
            # LLM classification was attempted (get_agent_client was called)
            assert role.name == "general"


# ---------------------------------------------------------------------------
# SemanticRouter keyword-boost: word-boundary matching
# Regression: substring matching let `'board'` (jira keyword) trigger on
# `'dashboard'` and steal release/my_dashboard routing for "Mein Dashboard".
# ---------------------------------------------------------------------------


class TestSemanticRouterKeywordBoostBoundary:
    """Keyword boost must match whole words, not arbitrary substrings."""

    def _build_router(self, keyword_boost_map):
        """Build a SemanticRouter with one embedding per role and a module-
        level _KEYWORD_BOOST set to ``keyword_boost_map``.

        Each role gets the same embedding vector so semantic similarity
        does not preselect one role over another — the boost is the only
        differentiator.
        """
        from services import semantic_router as sr_module

        sr = sr_module.SemanticRouter(threshold=0.75)
        same_vec = [1.0, 0.0, 0.0]
        sr._role_embeddings = {role: [same_vec] for role in keyword_boost_map}
        sr._initialized = True

        embed_response = MagicMock()
        embed_response.embedding = same_vec
        client = AsyncMock()
        client.embeddings = AsyncMock(return_value=embed_response)
        sr._ollama_client = client

        sr_module._KEYWORD_BOOST.clear()
        sr_module._KEYWORD_BOOST.update(
            {role: [k.lower() for k in kws] for role, kws in keyword_boost_map.items()}
        )
        return sr

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_dashboard_does_not_match_board_keyword(self):
        """`'board'` is a jira keyword — must not fire on `'dashboard'`."""
        from services import semantic_router as sr_module

        sr = self._build_router({
            "release": ["release", "releases"],
            "jira": ["jira", "ticket", "board", "epic"],
        })
        try:
            role, _sub, _sim = await sr.classify("Mein Dashboard")
        finally:
            sr_module._KEYWORD_BOOST.clear()

        # No keyword in {"mein", "dashboard"} matches release or jira → no
        # boost → caller falls back to LLM (returns None at threshold).
        # The crucial assertion: jira boost must NOT have fired.
        assert role != "jira"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_explicit_board_keyword_still_boosts(self):
        """Whole-word `'board'` must still trigger the jira boost."""
        from services import semantic_router as sr_module

        sr = self._build_router({
            "release": ["release"],
            "jira": ["board"],
        })
        try:
            role, _sub, sim = await sr.classify("Show me the board")
        finally:
            sr_module._KEYWORD_BOOST.clear()

        assert role == "jira"
        assert sim >= 0.75

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_german_umlaut_keyword_matches_word(self):
        """`'störung'` must match as a word in German messages."""
        from services import semantic_router as sr_module

        sr = self._build_router({
            "jira": ["ticket"],
            "itsm": ["störung"],
        })
        try:
            role, _sub, _sim = await sr.classify("Ich habe eine Störung gemeldet")
        finally:
            sr_module._KEYWORD_BOOST.clear()

        assert role == "itsm"


# ---------------------------------------------------------------------------
# Anti-dashboard guard + sub_intent inference on fast paths
# (follow-up to PR #384 review findings)
# ---------------------------------------------------------------------------


class TestInferSubIntent:
    """AgentRouter._infer_sub_intent: keyword-based sub_intent inference
    used by the Layer-1 entity-id path, Layer-2 continuity path, and the
    LLM prose-fallback path."""

    @pytest.mark.unit
    def test_status_report_wins_on_deliverable_query(self):
        from services.agent_router import AgentRouter
        defs = {
            "my_dashboard": {
                "de": "mein Dashboard, meine Aufgaben. NICHT verwenden fuer: Statusbericht erstellen",
            },
            "status_report": {
                "de": "Statusbericht, Release-Bericht, status report",
            },
        }
        assert AgentRouter._infer_sub_intent(
            "Statusbericht für Product A 1.3.5", defs, "de",
        ) == "status_report"

    @pytest.mark.unit
    def test_anti_dashboard_guard_rejects_false_positive(self):
        """A message asking for a status report must NOT be classified
        as my_dashboard even if 'Statusbericht' happens to appear in the
        my_dashboard description's NICHT clause — the keyword matcher
        can't parse negation."""
        from services.agent_router import AgentRouter
        defs = {
            "my_dashboard": {
                "de": "dashboard, meine aufgaben, NICHT fuer: Statusbericht erstellen, alle releases",
            },
        }
        assert AgentRouter._infer_sub_intent(
            "Statusbericht erstellen bitte", defs, "de",
        ) is None

    @pytest.mark.unit
    def test_my_dashboard_still_matches_genuine_query(self):
        from services.agent_router import AgentRouter
        defs = {
            "my_dashboard": {
                "de": "dashboard, meine aufgaben, was liegt bei mir an",
            },
        }
        assert AgentRouter._infer_sub_intent(
            "Was liegt bei mir an?", defs, "de",
        ) == "my_dashboard"

    @pytest.mark.unit
    def test_no_hits_returns_none(self):
        from services.agent_router import AgentRouter
        defs = {
            "my_dashboard": {"de": "dashboard, tasks"},
        }
        assert AgentRouter._infer_sub_intent(
            "Hello world", defs, "de",
        ) is None
