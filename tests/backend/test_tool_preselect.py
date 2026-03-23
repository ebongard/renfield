"""Tests for Tool Pre-Selection -- LLM-based tool filtering before agent loop."""

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure 'ollama' module is available
if "ollama" not in sys.modules:
    sys.modules["ollama"] = MagicMock()

from services.agent_service import AgentService
from services.agent_tools import AgentToolRegistry


def _make_mock_registry(tool_count: int) -> AgentToolRegistry:
    """Create a mock tool registry with N tools."""
    registry = MagicMock(spec=AgentToolRegistry)
    tools = {}
    for i in range(tool_count):
        name = f"mcp.server.tool_{i}"
        tool = MagicMock()
        tool.description = f"Description for tool {i}"
        tools[name] = tool
    registry._tools = tools
    registry.get_tool_names.return_value = list(tools.keys())
    registry.build_tools_prompt.return_value = "TOOLS: ..."
    return registry


def _make_agent(tool_count: int) -> AgentService:
    """Create an AgentService with a mock registry."""
    registry = _make_mock_registry(tool_count)
    role = MagicMock()
    role.max_steps = 5
    role.prompt_key = "agent_prompt"
    role.model = None
    role.ollama_url = None
    role.name = "general"
    agent = AgentService(registry, role=role)
    return agent


def _make_mock_client(response_text: str) -> AsyncMock:
    """Create a mock Ollama client returning fixed response."""
    mock_response = MagicMock()
    mock_response.message.content = response_text
    client = AsyncMock()
    client.chat = AsyncMock(return_value=mock_response)
    return client


class TestToolPreSelection:

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_skipped_when_few_tools(self):
        """Pre-selection skipped when <= 6 tools."""
        agent = _make_agent(5)
        result = await agent._preselect_tools("Schalte Licht ein", AsyncMock(), "model")
        assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_selects_tools_from_llm_response(self):
        """LLM response with valid tool names filters the registry."""
        agent = _make_agent(10)
        selected = ["mcp.server.tool_0", "mcp.server.tool_3", "mcp.server.tool_7"]
        client = _make_mock_client(json.dumps(selected))

        with patch("services.agent_service.prompt_manager") as pm, \
             patch("services.agent_service.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "preselect prompt"
            result = await agent._preselect_tools("Test message", client, "model")

        assert result is not None
        assert len(result) == 3
        assert "mcp.server.tool_0" in result
        assert "mcp.server.tool_3" in result
        assert "mcp.server.tool_7" in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_invalid_json_falls_back(self):
        """Invalid JSON response falls back to all tools."""
        agent = _make_agent(10)
        client = _make_mock_client("not valid json")

        with patch("services.agent_service.prompt_manager") as pm, \
             patch("services.agent_service.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "preselect prompt"
            result = await agent._preselect_tools("Test", client, "model")

        assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_empty_list_falls_back(self):
        """Empty JSON array falls back to all tools."""
        agent = _make_agent(10)
        client = _make_mock_client("[]")

        with patch("services.agent_service.prompt_manager") as pm, \
             patch("services.agent_service.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "preselect prompt"
            result = await agent._preselect_tools("Test", client, "model")

        assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_no_valid_tools_falls_back(self):
        """If LLM returns tool names not in registry, fall back."""
        agent = _make_agent(10)
        client = _make_mock_client('["nonexistent_tool_1", "nonexistent_tool_2"]')

        with patch("services.agent_service.prompt_manager") as pm, \
             patch("services.agent_service.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "preselect prompt"
            result = await agent._preselect_tools("Test", client, "model")

        assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_timeout_falls_back(self):
        """Timeout falls back gracefully."""
        import asyncio
        agent = _make_agent(10)
        client = AsyncMock()
        client.chat = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch("services.agent_service.prompt_manager") as pm, \
             patch("services.agent_service.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "preselect prompt"
            result = await agent._preselect_tools("Test", client, "model")

        assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_exception_falls_back(self):
        """Generic exception falls back gracefully."""
        agent = _make_agent(10)
        client = AsyncMock()
        client.chat = AsyncMock(side_effect=ConnectionError("offline"))

        with patch("services.agent_service.prompt_manager") as pm, \
             patch("services.agent_service.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "preselect prompt"
            result = await agent._preselect_tools("Test", client, "model")

        assert result is None
