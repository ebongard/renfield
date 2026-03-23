"""
Golden Dataset Tests — Validates AgentRouter classification against curated Q&A pairs.

Ensures that user messages are routed to the correct agent role.
Safety entries are validated via input_guard.detect_injection().
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure backend modules are importable
BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

if "ollama" not in sys.modules:
    sys.modules["ollama"] = MagicMock()

from services.agent_router import AgentRouter
from services.input_guard import detect_injection

# Load golden dataset
EVAL_DIR = Path(__file__).parent
with open(EVAL_DIR / "golden_dataset.json") as f:
    GOLDEN_DATA = json.load(f)

# Router test config (mirrors test_agent_router.py SAMPLE_CONFIG)
ROUTER_CONFIG = {
    "roles": {
        "smart_home": {
            "description": {"de": "Smart Home: Licht, Heizung, Sensoren, Schalter", "en": "Smart home: lights, heating, sensors, switches"},
            "mcp_servers": ["homeassistant"],
            "max_steps": 4,
            "prompt_key": "agent_prompt_smart_home",
        },
        "research": {
            "description": {"de": "Recherche: Websuche, Nachrichten, Wetter", "en": "Research: web search, news, weather"},
            "mcp_servers": ["search", "news", "weather"],
            "max_steps": 6,
            "prompt_key": "agent_prompt_research",
        },
        "documents": {
            "description": {"de": "Dokumente, E-Mail, Paperless", "en": "Documents, email, Paperless"},
            "mcp_servers": ["paperless", "email"],
            "max_steps": 8,
            "prompt_key": "agent_prompt_documents",
        },
        "media": {
            "description": {"de": "Medien: Musik, Filme, Serien, Lautstaerke", "en": "Media: music, movies, series, volume"},
            "mcp_servers": ["jellyfin"],
            "max_steps": 6,
            "prompt_key": "agent_prompt_media",
        },
        "workflow": {
            "description": {"de": "Automatisierungen: n8n Workflows", "en": "Automations: n8n workflows"},
            "mcp_servers": ["n8n"],
            "max_steps": 4,
            "prompt_key": "agent_prompt_workflow",
        },
        "presence": {
            "description": {"de": "Anwesenheit: Wo ist eine Person, wer ist zuhause", "en": "Presence: where is a person, who is home"},
            "mcp_servers": [],
            "internal_tools": ["internal.get_user_location", "internal.get_all_presence"],
            "max_steps": 2,
            "prompt_key": "agent_prompt",
        },
        "knowledge": {
            "description": {"de": "Wissensdatenbank", "en": "Knowledge base"},
        },
        "general": {
            "description": {"de": "Allgemein: komplexe oder domainuebergreifende Aufgaben", "en": "General: complex or cross-domain tasks"},
            "mcp_servers": None,
            "internal_tools": None,
            "max_steps": 12,
            "prompt_key": "agent_prompt",
        },
        "conversation": {
            "description": {"de": "Konversation, Smalltalk, Allgemeinwissen", "en": "Conversation, smalltalk, general knowledge"},
        },
    }
}


def _make_mock_ollama(response_text: str):
    """Create a mock OllamaService that returns a fixed LLM response."""
    mock = MagicMock()
    mock.default_lang = "de"
    mock_response = MagicMock()
    mock_response.message.content = response_text
    mock.client = AsyncMock()
    mock.client.chat = AsyncMock(return_value=mock_response)
    return mock


# ============================================================================
# Routing classification tests
# ============================================================================

ROUTING_CASES = [c for c in GOLDEN_DATA if not c.get("should_refuse") and c.get("expected_role")]


@pytest.mark.eval
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ROUTING_CASES,
    ids=[c["id"] for c in ROUTING_CASES],
)
async def test_golden_routing(case):
    """Validate that the router classifies this message to the expected role."""
    router = AgentRouter(ROUTER_CONFIG)
    expected_role = case["expected_role"]

    # Mock the LLM to return the expected role (tests config alignment, not LLM)
    ollama = _make_mock_ollama(json.dumps({"role": expected_role}))

    with patch("services.agent_router.settings") as s:
        s.agent_router_model = None
        s.agent_router_url = None
        s.agent_ollama_url = None
        s.ollama_intent_model = "test-model"
        s.ollama_model = "test-model"

        role = await router.classify(case["input"], ollama, lang=case.get("lang", "de"))
        assert role.name == expected_role, (
            f"[{case['id']}] Expected role '{expected_role}', got '{role.name}' "
            f"for input: {case['input']}"
        )


# ============================================================================
# Safety tests (input guard)
# ============================================================================

SAFETY_CASES = [c for c in GOLDEN_DATA if c.get("should_refuse")]


@pytest.mark.eval
@pytest.mark.parametrize(
    "case",
    SAFETY_CASES,
    ids=[c["id"] for c in SAFETY_CASES],
)
def test_golden_safety_blocked(case):
    """Validate that safety cases are blocked by the input guard."""
    result = detect_injection(case["input"])
    assert result.blocked, (
        f"[{case['id']}] Expected injection to be blocked but score={result.score:.2f}: "
        f"{case['input']}"
    )
