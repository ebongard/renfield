"""Tests for Cross-MCP Query Orchestrator."""

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if "ollama" not in sys.modules:
    sys.modules["ollama"] = MagicMock()

from services.orchestrator import QueryOrchestrator


def _make_role(name: str, has_loop: bool = True, servers: list | None = None):
    role = MagicMock()
    role.name = name
    role.has_agent_loop = has_loop
    role.description = {"de": f"{name} Beschreibung", "en": f"{name} description"}
    role.mcp_servers = servers
    role.internal_tools = None
    role.max_steps = 5
    role.prompt_key = "agent_prompt"
    role.model = None
    role.ollama_url = None
    return role


def _make_router(roles: list):
    router = MagicMock()
    router.roles = {r.name: r for r in roles}
    return router


def _make_ollama(response_text: str):
    mock = MagicMock()
    mock.default_lang = "de"
    mock_response = MagicMock()
    mock_response.message.content = response_text
    mock.client = AsyncMock()
    mock.client.chat = AsyncMock(return_value=mock_response)
    return mock


class TestDetectMultiDomain:

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_single_domain_returns_none(self):
        roles = [_make_role("smart_home"), _make_role("media"), _make_role("conversation", has_loop=False)]
        router = _make_router(roles)
        ollama = _make_ollama("null")
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_router_url = None
            s.agent_ollama_url = None
            s.agent_router_model = None
            s.ollama_intent_model = "test-model"
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            result = await orchestrator.detect_multi_domain("Wie wird das Wetter?", ollama)
            assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_multi_domain_detected(self):
        roles = [_make_role("smart_home", servers=["homeassistant"]), _make_role("media", servers=["jellyfin"])]
        router = _make_router(roles)
        response = json.dumps([
            {"role": "smart_home", "query": "Licht einschalten"},
            {"role": "media", "query": "Musik spielen"},
        ])
        ollama = _make_ollama(response)
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_router_url = None
            s.agent_ollama_url = None
            s.agent_router_model = None
            s.ollama_intent_model = "test-model"
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            result = await orchestrator.detect_multi_domain(
                "Mach Licht an und spiel Musik", ollama
            )
            assert result is not None
            assert len(result) == 2
            assert result[0]["role"] == "smart_home"
            assert result[1]["role"] == "media"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_invalid_role_filtered(self):
        roles = [_make_role("smart_home")]
        router = _make_router(roles)
        response = json.dumps([
            {"role": "smart_home", "query": "Licht an"},
            {"role": "nonexistent", "query": "something"},
        ])
        ollama = _make_ollama(response)
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_router_url = None
            s.agent_ollama_url = None
            s.agent_router_model = None
            s.ollama_intent_model = "test-model"
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            result = await orchestrator.detect_multi_domain("test", ollama)
            # Only 1 valid role → not multi-domain
            assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        import asyncio
        roles = [_make_role("smart_home"), _make_role("media")]
        router = _make_router(roles)
        ollama = MagicMock()
        ollama.client = AsyncMock()
        ollama.client.chat = AsyncMock(side_effect=asyncio.TimeoutError())
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_router_url = None
            s.agent_ollama_url = None
            s.agent_router_model = None
            s.ollama_intent_model = "test-model"
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            result = await orchestrator.detect_multi_domain("test", ollama)
            assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_json_parse_error_returns_none(self):
        roles = [_make_role("smart_home"), _make_role("media")]
        router = _make_router(roles)
        ollama = _make_ollama("not valid json at all")
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_router_url = None
            s.agent_ollama_url = None
            s.agent_router_model = None
            s.ollama_intent_model = "test-model"
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            result = await orchestrator.detect_multi_domain("test", ollama)
            assert result is None
