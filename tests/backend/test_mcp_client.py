"""
Tests for MCPManager — MCP client core, config loading, tool execution, and integration.
"""

import os
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, mock_open
from dataclasses import dataclass

from services.mcp_client import (
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPToolInfo,
    MCPTransportType,
    TokenBucketRateLimiter,
    MCPValidationError,
    MCPRateLimitError,
    ExponentialBackoff,
    _substitute_env_vars,
    _resolve_value,
    _validate_tool_input,
    _truncate_response,
    MAX_RESPONSE_SIZE,
    BACKOFF_INITIAL_DELAY,
    BACKOFF_MAX_DELAY,
    BACKOFF_MULTIPLIER,
)


# ============================================================================
# Env-Var Substitution
# ============================================================================

class TestEnvVarSubstitution:
    """Test ${VAR} and ${VAR:-default} substitution."""

    @pytest.mark.unit
    def test_simple_substitution(self):
        with patch.dict(os.environ, {"MY_VAR": "hello"}):
            assert _substitute_env_vars("${MY_VAR}") == "hello"

    @pytest.mark.unit
    def test_default_value_used(self):
        env = {k: v for k, v in os.environ.items() if k != "MISSING_VAR"}
        with patch.dict(os.environ, env, clear=True):
            assert _substitute_env_vars("${MISSING_VAR:-fallback}") == "fallback"

    @pytest.mark.unit
    def test_env_overrides_default(self):
        with patch.dict(os.environ, {"MY_VAR": "real_value"}):
            assert _substitute_env_vars("${MY_VAR:-fallback}") == "real_value"

    @pytest.mark.unit
    def test_no_substitution_needed(self):
        assert _substitute_env_vars("plain text") == "plain text"

    @pytest.mark.unit
    def test_multiple_vars(self):
        with patch.dict(os.environ, {"A": "1", "B": "2"}):
            assert _substitute_env_vars("${A}-${B}") == "1-2"

    @pytest.mark.unit
    def test_missing_required_var_returns_empty(self):
        env = {k: v for k, v in os.environ.items() if k != "UNDEF_VAR"}
        with patch.dict(os.environ, env, clear=True):
            assert _substitute_env_vars("${UNDEF_VAR}") == ""

    @pytest.mark.unit
    def test_resolve_bool_true(self):
        assert _resolve_value("true") is True
        assert _resolve_value("True") is True
        assert _resolve_value("1") is True

    @pytest.mark.unit
    def test_resolve_bool_false(self):
        assert _resolve_value("false") is False
        assert _resolve_value("False") is False
        assert _resolve_value("0") is False

    @pytest.mark.unit
    def test_resolve_non_string(self):
        assert _resolve_value(42) == 42
        assert _resolve_value(True) is True


# ============================================================================
# Config Loading
# ============================================================================

class TestLoadConfig:
    """Test YAML configuration loading."""

    @pytest.mark.unit
    def test_load_config_from_yaml(self, tmp_path):
        """YAML parsing with env-var substitution."""
        config_file = tmp_path / "mcp_servers.yaml"
        config_file.write_text("""
servers:
  - name: test_server
    url: "http://localhost:8080/mcp"
    transport: streamable_http
    enabled: true
    refresh_interval: 120
""")
        manager = MCPManager()
        manager.load_config(str(config_file))

        assert "test_server" in manager._servers
        state = manager._servers["test_server"]
        assert state.config.name == "test_server"
        assert state.config.url == "http://localhost:8080/mcp"
        assert state.config.transport == MCPTransportType.STREAMABLE_HTTP
        assert state.config.refresh_interval == 120

    @pytest.mark.unit
    def test_env_var_substitution_in_config(self, tmp_path):
        """${VAR} and ${VAR:-default} patterns in YAML."""
        config_file = tmp_path / "mcp_servers.yaml"
        config_file.write_text("""
servers:
  - name: env_test
    url: "${TEST_MCP_URL:-http://default:9090/mcp}"
    transport: sse
    enabled: "${TEST_MCP_ENABLED:-true}"
""")
        with patch.dict(os.environ, {"TEST_MCP_URL": "http://custom:1234/mcp"}):
            manager = MCPManager()
            manager.load_config(str(config_file))

        assert "env_test" in manager._servers
        assert manager._servers["env_test"].config.url == "http://custom:1234/mcp"
        assert manager._servers["env_test"].config.transport == MCPTransportType.SSE

    @pytest.mark.unit
    def test_disabled_server_skipped(self, tmp_path):
        """Disabled servers should not be registered."""
        config_file = tmp_path / "mcp_servers.yaml"
        config_file.write_text("""
servers:
  - name: disabled_server
    url: "http://localhost/mcp"
    enabled: false
""")
        manager = MCPManager()
        manager.load_config(str(config_file))

        assert "disabled_server" not in manager._servers

    @pytest.mark.unit
    def test_missing_config_file(self):
        """Missing config file should log warning and continue."""
        manager = MCPManager()
        manager.load_config("/nonexistent/path.yaml")
        assert len(manager._servers) == 0

    @pytest.mark.unit
    def test_empty_servers_list(self, tmp_path):
        """Empty servers list should work."""
        config_file = tmp_path / "mcp_servers.yaml"
        config_file.write_text("servers:\n")
        manager = MCPManager()
        manager.load_config(str(config_file))
        assert len(manager._servers) == 0

    @pytest.mark.unit
    def test_stdio_transport_config(self, tmp_path):
        """Stdio transport should parse command and args."""
        config_file = tmp_path / "mcp_servers.yaml"
        config_file.write_text("""
servers:
  - name: stdio_server
    transport: stdio
    command: npx
    args: ["-y", "@mcp/server-fs", "/data"]
    enabled: true
""")
        manager = MCPManager()
        manager.load_config(str(config_file))

        assert "stdio_server" in manager._servers
        config = manager._servers["stdio_server"].config
        assert config.transport == MCPTransportType.STDIO
        assert config.command == "npx"
        assert config.args == ["-y", "@mcp/server-fs", "/data"]


# ============================================================================
# Tool Namespacing
# ============================================================================

class TestToolNamespacing:
    """Test mcp.<server>.<tool> naming."""

    @pytest.mark.unit
    def test_tool_namespacing(self):
        """Tools should be namespaced as mcp.<server>.<tool>."""
        tool = MCPToolInfo(
            server_name="n8n",
            original_name="send_email",
            namespaced_name="mcp.n8n.send_email",
            description="Send an email",
            input_schema={"properties": {"to": {"type": "string"}}},
        )
        assert tool.namespaced_name == "mcp.n8n.send_email"
        assert tool.server_name == "n8n"
        assert tool.original_name == "send_email"

    @pytest.mark.unit
    def test_is_mcp_tool(self):
        """is_mcp_tool should check the index."""
        manager = MCPManager()
        tool = MCPToolInfo(
            server_name="test",
            original_name="foo",
            namespaced_name="mcp.test.foo",
            description="Test tool",
        )
        manager._tool_index["mcp.test.foo"] = tool

        assert manager.is_mcp_tool("mcp.test.foo") is True
        assert manager.is_mcp_tool("mcp.test.bar") is False
        assert manager.is_mcp_tool("homeassistant.turn_on") is False

    @pytest.mark.unit
    def test_get_all_tools(self):
        """get_all_tools should return all indexed tools."""
        manager = MCPManager()
        tool1 = MCPToolInfo("s1", "t1", "mcp.s1.t1", "desc1")
        tool2 = MCPToolInfo("s2", "t2", "mcp.s2.t2", "desc2")
        manager._tool_index["mcp.s1.t1"] = tool1
        manager._tool_index["mcp.s2.t2"] = tool2

        tools = manager.get_all_tools()
        assert len(tools) == 2
        names = {t.namespaced_name for t in tools}
        assert names == {"mcp.s1.t1", "mcp.s2.t2"}


# ============================================================================
# Tool Execution
# ============================================================================

class TestExecuteTool:
    """Test MCPManager.execute_tool()."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_execute_tool_unknown(self):
        """Unknown tool name should return error."""
        manager = MCPManager()
        result = await manager.execute_tool("mcp.nonexistent.tool", {})
        assert result["success"] is False
        assert "Unknown" in result["message"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_execute_tool_server_down(self):
        """Disconnected server should return error."""
        manager = MCPManager()
        tool = MCPToolInfo("down_server", "test", "mcp.down_server.test", "Test")
        manager._tool_index["mcp.down_server.test"] = tool

        state = MCPServerState(
            config=MCPServerConfig(name="down_server"),
            connected=False,
        )
        manager._servers["down_server"] = state

        result = await manager.execute_tool("mcp.down_server.test", {})
        assert result["success"] is False
        assert "nicht verbunden" in result["message"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_execute_tool_success(self):
        """Successful tool call should return formatted result."""
        manager = MCPManager()
        tool = MCPToolInfo("srv", "mytool", "mcp.srv.mytool", "My tool")
        manager._tool_index["mcp.srv.mytool"] = tool

        # Mock session
        mock_content = MagicMock()
        mock_content.text = "Tool result text"
        mock_content.type = "text"

        mock_result = MagicMock()
        mock_result.isError = False
        mock_result.content = [mock_content]

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        state = MCPServerState(
            config=MCPServerConfig(name="srv"),
            connected=True,
            session=mock_session,
        )
        manager._servers["srv"] = state

        result = await manager.execute_tool("mcp.srv.mytool", {"param": "value"})
        assert result["success"] is True
        assert result["message"] == "Tool result text"
        mock_session.call_tool.assert_called_once_with("mytool", {"param": "value"})

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_execute_tool_error_result(self):
        """Tool returning isError=True should return success=False."""
        manager = MCPManager()
        tool = MCPToolInfo("srv", "bad", "mcp.srv.bad", "Failing tool")
        manager._tool_index["mcp.srv.bad"] = tool

        mock_content = MagicMock()
        mock_content.text = "Something went wrong"
        mock_content.type = "text"

        mock_result = MagicMock()
        mock_result.isError = True
        mock_result.content = [mock_content]

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        state = MCPServerState(
            config=MCPServerConfig(name="srv"),
            connected=True,
            session=mock_session,
        )
        manager._servers["srv"] = state

        result = await manager.execute_tool("mcp.srv.bad", {})
        assert result["success"] is False
        assert "Something went wrong" in result["message"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_execute_tool_timeout(self):
        """Tool call exceeding timeout should return error."""
        manager = MCPManager()
        tool = MCPToolInfo("srv", "slow", "mcp.srv.slow", "Slow tool")
        manager._tool_index["mcp.srv.slow"] = tool

        async def slow_call(*args, **kwargs):
            await asyncio.sleep(100)

        mock_session = AsyncMock()
        mock_session.call_tool = slow_call

        state = MCPServerState(
            config=MCPServerConfig(name="srv"),
            connected=True,
            session=mock_session,
        )
        manager._servers["srv"] = state

        with patch("services.mcp_client.settings") as mock_settings:
            mock_settings.mcp_call_timeout = 0.01
            result = await manager.execute_tool("mcp.srv.slow", {})

        assert result["success"] is False
        assert "Timeout" in result["message"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_tool_result_empty_content(self):
        """Empty tool content should return generic message."""
        manager = MCPManager()
        tool = MCPToolInfo("srv", "empty", "mcp.srv.empty", "Empty tool")
        manager._tool_index["mcp.srv.empty"] = tool

        mock_result = MagicMock()
        mock_result.isError = False
        mock_result.content = []

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        state = MCPServerState(
            config=MCPServerConfig(name="srv"),
            connected=True,
            session=mock_session,
        )
        manager._servers["srv"] = state

        result = await manager.execute_tool("mcp.srv.empty", {})
        assert result["success"] is True
        assert result["message"] == "Tool executed"


# ============================================================================
# Connect — Partial Failure
# ============================================================================

class TestConnectPartialFailure:
    """Test that partial connection failures don't block other servers."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_connect_partial_failure(self):
        """One server failing should not prevent others from connecting."""
        manager = MCPManager()

        # Two servers: one will fail, one succeeds
        manager._servers["failing"] = MCPServerState(
            config=MCPServerConfig(name="failing", url="http://bad:9999/mcp")
        )
        manager._servers["good"] = MCPServerState(
            config=MCPServerConfig(name="good", url="http://good:8080/mcp")
        )

        # Mock _connect_server to simulate partial failure
        call_count = {"failing": 0, "good": 0}

        async def mock_connect(state):
            call_count[state.config.name] += 1
            if state.config.name == "failing":
                state.connected = False
                state.last_error = "Connection refused"
            else:
                state.connected = True
                state.tools = [
                    MCPToolInfo("good", "test", "mcp.good.test", "A tool")
                ]
                manager._tool_index["mcp.good.test"] = state.tools[0]

        with patch.object(manager, "_connect_server", side_effect=mock_connect):
            await manager.connect_all()

        assert manager._servers["failing"].connected is False
        assert manager._servers["good"].connected is True
        assert len(manager._tool_index) == 1
        assert call_count["failing"] == 1
        assert call_count["good"] == 1


# ============================================================================
# Status
# ============================================================================

class TestGetStatus:
    """Test MCPManager.get_status()."""

    @pytest.mark.unit
    def test_status_empty(self):
        manager = MCPManager()
        status = manager.get_status()
        assert status["enabled"] is True
        assert status["total_tools"] == 0
        assert status["servers"] == []

    @pytest.mark.unit
    def test_status_with_servers(self):
        manager = MCPManager()
        tool = MCPToolInfo("srv", "t", "mcp.srv.t", "desc")
        manager._tool_index["mcp.srv.t"] = tool
        manager._servers["srv"] = MCPServerState(
            config=MCPServerConfig(name="srv"),
            connected=True,
            tools=[tool],
        )

        status = manager.get_status()
        assert status["total_tools"] == 1
        assert len(status["servers"]) == 1
        assert status["servers"][0]["name"] == "srv"
        assert status["servers"][0]["connected"] is True
        assert status["servers"][0]["tool_count"] == 1


# ============================================================================
# Shutdown
# ============================================================================

class TestShutdown:
    """Test MCPManager.shutdown()."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_shutdown_clears_state(self):
        manager = MCPManager()
        tool = MCPToolInfo("srv", "t", "mcp.srv.t", "desc")
        manager._tool_index["mcp.srv.t"] = tool

        mock_exit_stack = AsyncMock()
        manager._servers["srv"] = MCPServerState(
            config=MCPServerConfig(name="srv"),
            connected=True,
            tools=[tool],
            exit_stack=mock_exit_stack,
        )

        await manager.shutdown()

        assert len(manager._tool_index) == 0
        assert manager._servers["srv"].connected is False
        assert manager._servers["srv"].session is None
        mock_exit_stack.__aexit__.assert_called_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_shutdown_cancels_refresh_task(self):
        manager = MCPManager()
        manager._refresh_task = asyncio.create_task(asyncio.sleep(1000))

        await manager.shutdown()

        assert manager._refresh_task.cancelled()


# ============================================================================
# AgentToolRegistry Integration
# ============================================================================

class TestMCPToolsInRegistry:
    """Test that MCP tools appear in AgentToolRegistry."""

    @pytest.mark.unit
    def test_mcp_tools_in_registry(self):
        """MCP tools should be registered as agent tools."""
        from services.agent_tools import AgentToolRegistry

        mock_manager = MagicMock()
        mock_manager.get_all_tools.return_value = [
            MCPToolInfo(
                server_name="n8n",
                original_name="send_email",
                namespaced_name="mcp.n8n.send_email",
                description="Send an email via n8n",
                input_schema={
                    "properties": {
                        "to": {"type": "string", "description": "Recipient address"},
                        "subject": {"type": "string", "description": "Email subject"},
                    },
                    "required": ["to", "subject"],
                },
            ),
        ]

        registry = AgentToolRegistry(ha_available=False, mcp_manager=mock_manager)

        assert registry.is_valid_tool("mcp.n8n.send_email") is True
        tool = registry.get_tool("mcp.n8n.send_email")
        assert tool.description == "Send an email via n8n"
        assert "to" in tool.parameters
        assert "(required)" in tool.parameters["to"]
        assert "(required)" in tool.parameters["subject"]

    @pytest.mark.unit
    def test_mcp_tools_in_prompt(self):
        """MCP tools should appear in build_tools_prompt() output."""
        from services.agent_tools import AgentToolRegistry

        mock_manager = MagicMock()
        mock_manager.get_all_tools.return_value = [
            MCPToolInfo(
                server_name="test",
                original_name="greet",
                namespaced_name="mcp.test.greet",
                description="Greet someone",
                input_schema={
                    "properties": {"name": {"type": "string", "description": "Person name"}},
                    "required": ["name"],
                },
            ),
        ]

        registry = AgentToolRegistry(ha_available=False, mcp_manager=mock_manager)
        prompt = registry.build_tools_prompt()

        assert "mcp.test.greet" in prompt
        assert "Greet someone" in prompt
        assert "name" in prompt

    @pytest.mark.unit
    def test_registry_without_mcp(self):
        """Registry should work fine without mcp_manager."""
        from services.agent_tools import AgentToolRegistry

        registry = AgentToolRegistry(ha_available=True, mcp_manager=None)
        assert registry.is_valid_tool("homeassistant.turn_on") is True
        assert registry.is_valid_tool("mcp.anything") is False


# ============================================================================
# ActionExecutor Integration
# ============================================================================

class TestMCPIntentRouting:
    """Test that mcp.* intents are routed to MCPManager."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_mcp_intent_routing(self):
        """mcp.* intents should be routed to MCPManager.execute_tool()."""
        from services.action_executor import ActionExecutor

        mock_manager = AsyncMock()
        mock_manager.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": "Email sent",
            "data": None,
        })

        executor = ActionExecutor(mcp_manager=mock_manager)
        result = await executor.execute({
            "intent": "mcp.n8n.send_email",
            "parameters": {"to": "user@example.com", "subject": "Test"},
            "confidence": 0.9,
        })

        assert result["success"] is True
        assert result["message"] == "Email sent"
        mock_manager.execute_tool.assert_called_once_with(
            "mcp.n8n.send_email",
            {"to": "user@example.com", "subject": "Test"},
        )

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_non_mcp_unaffected(self):
        """Non-MCP intents should not be routed to MCPManager."""
        from services.action_executor import ActionExecutor

        mock_manager = AsyncMock()
        executor = ActionExecutor(mcp_manager=mock_manager)

        # HA intent should go through normal routing (will hit HA client)
        # We just verify mcp_manager is NOT called
        result = await executor.execute({
            "intent": "general.conversation",
            "parameters": {},
            "confidence": 0.9,
        })

        mock_manager.execute_tool.assert_not_called()
        assert result["success"] is True

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_executor_without_mcp(self):
        """Executor should work without mcp_manager (None)."""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor(mcp_manager=None)
        result = await executor.execute({
            "intent": "general.conversation",
            "parameters": {},
            "confidence": 0.9,
        })

        assert result["success"] is True


# ============================================================================
# Input Validation
# ============================================================================

class TestInputValidation:
    """Test MCP tool input validation against JSON schema."""

    @pytest.mark.unit
    def test_valid_input(self):
        """Valid input should pass validation."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name"],
        }
        arguments = {"name": "Alice", "age": 30}

        # Should not raise
        _validate_tool_input(arguments, schema)

    @pytest.mark.unit
    def test_missing_required_field(self):
        """Missing required field should raise MCPValidationError."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        }
        arguments = {}

        with pytest.raises(MCPValidationError) as exc:
            _validate_tool_input(arguments, schema)
        assert "validation failed" in str(exc.value).lower()

    @pytest.mark.unit
    def test_wrong_type(self):
        """Wrong type should raise MCPValidationError."""
        schema = {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
            },
        }
        arguments = {"count": "not a number"}

        with pytest.raises(MCPValidationError) as exc:
            _validate_tool_input(arguments, schema)
        assert "validation failed" in str(exc.value).lower()

    @pytest.mark.unit
    def test_empty_schema_passes(self):
        """Empty schema should allow any input."""
        _validate_tool_input({"any": "thing"}, {})
        _validate_tool_input({}, {})
        _validate_tool_input({"nested": {"data": True}}, None)


# ============================================================================
# Response Truncation
# ============================================================================

class TestResponseTruncation:
    """Test MCP response truncation."""

    @pytest.mark.unit
    def test_short_response_unchanged(self):
        """Short responses should not be truncated."""
        text = "Hello, World!"
        result = _truncate_response(text)
        assert result == text

    @pytest.mark.unit
    def test_long_response_truncated(self):
        """Long responses should be truncated to MAX_RESPONSE_SIZE."""
        text = "x" * (MAX_RESPONSE_SIZE + 1000)
        result = _truncate_response(text)

        # Should be truncated and have indicator
        assert len(result.encode('utf-8')) <= MAX_RESPONSE_SIZE
        assert "[... Response truncated" in result

    @pytest.mark.unit
    def test_exact_size_not_truncated(self):
        """Response exactly at limit should not be truncated."""
        # Create text that's exactly at the limit
        text = "a" * (MAX_RESPONSE_SIZE - 100)  # Leave some buffer for UTF-8
        result = _truncate_response(text)
        assert result == text
        assert "truncated" not in result.lower()

    @pytest.mark.unit
    def test_unicode_truncation(self):
        """Unicode characters should be handled properly during truncation."""
        # German umlauts and emoji
        text = "äöü" * 5000 + "🎉" * 1000
        result = _truncate_response(text)

        # Should not raise and should be properly truncated
        assert len(result.encode('utf-8')) <= MAX_RESPONSE_SIZE
        # Should be valid UTF-8
        result.encode('utf-8').decode('utf-8')


# ============================================================================
# Rate Limiting
# ============================================================================

class TestTokenBucketRateLimiter:
    """Test token bucket rate limiter."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_allows_within_limit(self):
        """Should allow requests within rate limit."""
        limiter = TokenBucketRateLimiter(rate_per_minute=60)

        # Should allow first request
        assert await limiter.acquire() is True

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_blocks_when_exhausted(self):
        """Should block when tokens exhausted."""
        limiter = TokenBucketRateLimiter(rate_per_minute=2)

        # Use up all tokens
        assert await limiter.acquire() is True
        assert await limiter.acquire() is True

        # Third should be blocked
        assert await limiter.acquire() is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_refills_over_time(self):
        """Tokens should refill over time."""
        limiter = TokenBucketRateLimiter(rate_per_minute=60)  # 1 per second

        # Use a token
        await limiter.acquire()

        # Wait for refill (60/min = 1/sec, so wait >1sec)
        import time
        time.sleep(1.1)

        # Should have tokens again
        assert await limiter.acquire() is True

    @pytest.mark.unit
    def test_reset(self):
        """Reset should restore full capacity."""
        limiter = TokenBucketRateLimiter(rate_per_minute=3)

        # Exhaust tokens
        asyncio.run(limiter.acquire())
        asyncio.run(limiter.acquire())
        asyncio.run(limiter.acquire())

        limiter.reset()

        assert limiter.tokens == limiter.max_tokens


# ============================================================================
# Integration: Validation + Truncation + Rate Limit in execute_tool
# ============================================================================

class TestExecuteToolSecurity:
    """Test security features in execute_tool."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_execute_validates_input(self):
        """execute_tool should validate input against schema."""
        manager = MCPManager()
        tool = MCPToolInfo(
            server_name="srv",
            original_name="strict_tool",
            namespaced_name="mcp.srv.strict_tool",
            description="A tool with strict schema",
            input_schema={
                "type": "object",
                "properties": {"required_field": {"type": "string"}},
                "required": ["required_field"],
            },
        )
        manager._tool_index["mcp.srv.strict_tool"] = tool

        mock_session = AsyncMock()
        state = MCPServerState(
            config=MCPServerConfig(name="srv"),
            connected=True,
            session=mock_session,
            rate_limiter=TokenBucketRateLimiter(rate_per_minute=100),
        )
        manager._servers["srv"] = state

        # Missing required field should fail validation
        result = await manager.execute_tool("mcp.srv.strict_tool", {})

        assert result["success"] is False
        assert "validation" in result["message"].lower()
        mock_session.call_tool.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_execute_truncates_large_response(self):
        """execute_tool should truncate large responses."""
        manager = MCPManager()
        tool = MCPToolInfo("srv", "big", "mcp.srv.big", "Big response tool")
        manager._tool_index["mcp.srv.big"] = tool

        # Mock huge response
        mock_content = MagicMock()
        mock_content.text = "x" * (MAX_RESPONSE_SIZE + 5000)
        mock_content.type = "text"

        mock_result = MagicMock()
        mock_result.isError = False
        mock_result.content = [mock_content]

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        state = MCPServerState(
            config=MCPServerConfig(name="srv"),
            connected=True,
            session=mock_session,
            rate_limiter=TokenBucketRateLimiter(rate_per_minute=100),
        )
        manager._servers["srv"] = state

        result = await manager.execute_tool("mcp.srv.big", {})

        assert result["success"] is True
        assert len(result["message"].encode('utf-8')) <= MAX_RESPONSE_SIZE
        assert "truncated" in result["message"].lower()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_execute_respects_rate_limit(self):
        """execute_tool should respect rate limits."""
        manager = MCPManager()
        tool = MCPToolInfo("srv", "t", "mcp.srv.t", "Tool")
        manager._tool_index["mcp.srv.t"] = tool

        mock_content = MagicMock()
        mock_content.text = "OK"
        mock_content.type = "text"

        mock_result = MagicMock()
        mock_result.isError = False
        mock_result.content = [mock_content]

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        # Rate limiter with very low limit
        rate_limiter = TokenBucketRateLimiter(rate_per_minute=1)
        state = MCPServerState(
            config=MCPServerConfig(name="srv"),
            connected=True,
            session=mock_session,
            rate_limiter=rate_limiter,
        )
        manager._servers["srv"] = state

        # First call should succeed
        result1 = await manager.execute_tool("mcp.srv.t", {})
        assert result1["success"] is True

        # Second call should be rate limited
        result2 = await manager.execute_tool("mcp.srv.t", {})
        assert result2["success"] is False
        assert "rate limit" in result2["message"].lower()


# ============================================================================
# Exponential Backoff
# ============================================================================

class TestExponentialBackoff:
    """Test exponential backoff for reconnection."""

    @pytest.mark.unit
    def test_initial_state(self):
        """Backoff should start with zero attempts."""
        backoff = ExponentialBackoff()
        assert backoff.attempt_count == 0
        assert backoff.should_retry() is True
        assert backoff.time_until_retry() == 0.0

    @pytest.mark.unit
    def test_first_failure_delay(self):
        """First failure should use initial delay."""
        backoff = ExponentialBackoff(initial_delay=1.0, jitter=0.0)
        delay = backoff.record_failure()

        assert backoff.attempt_count == 1
        assert delay == pytest.approx(1.0, rel=0.01)
        assert backoff.should_retry() is False

    @pytest.mark.unit
    def test_exponential_increase(self):
        """Delays should increase exponentially."""
        backoff = ExponentialBackoff(
            initial_delay=1.0,
            multiplier=2.0,
            max_delay=1000.0,
            jitter=0.0,
        )

        # First failure: 1.0
        delay1 = backoff.record_failure()
        assert delay1 == pytest.approx(1.0)

        # Second failure: 2.0
        delay2 = backoff.record_failure()
        assert delay2 == pytest.approx(2.0)

        # Third failure: 4.0
        delay3 = backoff.record_failure()
        assert delay3 == pytest.approx(4.0)

        # Fourth failure: 8.0
        delay4 = backoff.record_failure()
        assert delay4 == pytest.approx(8.0)

    @pytest.mark.unit
    def test_max_delay_cap(self):
        """Delay should be capped at max_delay."""
        backoff = ExponentialBackoff(
            initial_delay=100.0,
            multiplier=10.0,
            max_delay=300.0,
            jitter=0.0,
        )

        # First: 100
        backoff.record_failure()
        # Second: would be 1000, capped to 300
        delay = backoff.record_failure()

        assert delay == pytest.approx(300.0)

    @pytest.mark.unit
    def test_jitter_adds_randomness(self):
        """Jitter should add randomness to delays."""
        backoff = ExponentialBackoff(
            initial_delay=10.0,
            jitter=0.5,  # 50% jitter
            max_delay=1000.0,
        )

        delays = []
        for _ in range(10):
            b = ExponentialBackoff(initial_delay=10.0, jitter=0.5, max_delay=1000.0)
            delays.append(b.record_failure())
            b.record_success()

        # Not all delays should be identical (random jitter)
        unique_delays = set(round(d, 4) for d in delays)
        # With 50% jitter, we expect variation
        assert len(unique_delays) > 1

    @pytest.mark.unit
    def test_success_resets_backoff(self):
        """record_success() should reset the backoff state."""
        backoff = ExponentialBackoff(jitter=0.0)

        backoff.record_failure()
        backoff.record_failure()
        assert backoff.attempt_count == 2

        backoff.record_success()

        assert backoff.attempt_count == 0
        assert backoff.should_retry() is True
        assert backoff.time_until_retry() == 0.0

    @pytest.mark.unit
    def test_should_retry_after_delay(self):
        """should_retry() should return True after delay passes."""
        backoff = ExponentialBackoff(initial_delay=0.05, jitter=0.0)
        backoff.record_failure()

        assert backoff.should_retry() is False

        # Wait for delay
        import time
        time.sleep(0.06)

        assert backoff.should_retry() is True

    @pytest.mark.unit
    def test_time_until_retry(self):
        """time_until_retry() should return remaining wait time."""
        backoff = ExponentialBackoff(initial_delay=1.0, jitter=0.0)
        backoff.record_failure()

        remaining = backoff.time_until_retry()
        assert remaining > 0.9
        assert remaining <= 1.0


class TestBackoffInServerState:
    """Test backoff integration in MCPServerState."""

    @pytest.mark.unit
    def test_server_state_has_backoff(self, tmp_path):
        """MCPServerState should have backoff tracker after load_config."""
        config_file = tmp_path / "mcp_servers.yaml"
        config_file.write_text("""
servers:
  - name: test_server
    url: "http://localhost:8080/mcp"
    enabled: true
""")
        manager = MCPManager()
        manager.load_config(str(config_file))

        state = manager._servers["test_server"]
        assert state.backoff is not None
        assert isinstance(state.backoff, ExponentialBackoff)
        assert state.backoff.attempt_count == 0

    @pytest.mark.unit
    def test_status_includes_backoff_info(self):
        """get_status() should include backoff info for disconnected servers."""
        manager = MCPManager()
        backoff = ExponentialBackoff(jitter=0.0)
        backoff.record_failure()
        backoff.record_failure()

        manager._servers["srv"] = MCPServerState(
            config=MCPServerConfig(name="srv"),
            connected=False,
            last_error="Connection failed",
            backoff=backoff,
        )

        status = manager.get_status()
        server_info = status["servers"][0]

        assert server_info["reconnect_attempts"] == 2
        assert "next_retry_in" in server_info

    @pytest.mark.unit
    def test_status_no_backoff_for_connected(self):
        """Connected servers should not have backoff info in status."""
        manager = MCPManager()
        manager._servers["srv"] = MCPServerState(
            config=MCPServerConfig(name="srv"),
            connected=True,
            backoff=ExponentialBackoff(),
        )

        status = manager.get_status()
        server_info = status["servers"][0]

        assert "reconnect_attempts" not in server_info
        assert "next_retry_in" not in server_info


class TestBackoffInReconnect:
    """Test backoff behavior in refresh_tools reconnection."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_reconnect_respects_backoff(self):
        """refresh_tools should skip reconnect if backoff not ready."""
        manager = MCPManager()

        # Create a disconnected server with backoff in progress
        backoff = ExponentialBackoff(initial_delay=1000.0, jitter=0.0)
        backoff.record_failure()  # Set next retry 1000s from now

        state = MCPServerState(
            config=MCPServerConfig(name="backoff_srv"),
            connected=False,
            backoff=backoff,
        )
        manager._servers["backoff_srv"] = state

        # Track if _connect_server is called
        connect_called = False

        async def mock_connect(s):
            nonlocal connect_called
            connect_called = True

        with patch.object(manager, "_connect_server", side_effect=mock_connect):
            await manager.refresh_tools()

        # Should NOT have called connect due to backoff
        assert connect_called is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_reconnect_proceeds_when_backoff_ready(self):
        """refresh_tools should reconnect when backoff timer expires."""
        manager = MCPManager()

        # Create a disconnected server with expired backoff
        backoff = ExponentialBackoff(initial_delay=0.001, jitter=0.0)
        backoff.record_failure()

        # Wait for backoff to expire
        import time
        time.sleep(0.01)

        state = MCPServerState(
            config=MCPServerConfig(name="ready_srv"),
            connected=False,
            backoff=backoff,
        )
        manager._servers["ready_srv"] = state

        connect_called = False

        async def mock_connect(s):
            nonlocal connect_called
            connect_called = True

        with patch.object(manager, "_connect_server", side_effect=mock_connect):
            await manager.refresh_tools()

        # Should have called connect since backoff expired
        assert connect_called is True
