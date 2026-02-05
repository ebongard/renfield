"""
Real E2E Test — Renfield creates Morning Briefing workflow in n8n.

No mocks — real MCP stdio subprocess, real n8n API, real workflow visible in the n8n UI.

Architecture:
    Test → MCPManager (real) → stdio subprocess (npx -y n8n-mcp) → n8n API

Prerequisites (checked at test collection time):
    - .mcp.json with n8n config (N8N_API_URL + N8N_API_KEY)
    - npx available on PATH
    - n8n instance reachable
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from services.action_executor import ActionExecutor
from services.mcp_client import (
    ExponentialBackoff,
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPTransportType,
    TokenBucketRateLimiter,
)

# ============================================================================
# Skip conditions
# ============================================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MCP_JSON_PATH = PROJECT_ROOT / ".mcp.json"

def _load_mcp_config() -> dict | None:
    """Load n8n config from .mcp.json, return None if unavailable."""
    if not MCP_JSON_PATH.exists():
        return None
    try:
        data = json.loads(MCP_JSON_PATH.read_text())
        n8n_cfg = data.get("mcpServers", {}).get("n8n", {})
        env = n8n_cfg.get("env", {})
        url = env.get("N8N_API_URL")
        key = env.get("N8N_API_KEY")
        if url and key:
            return {"url": url, "key": key, "command": n8n_cfg.get("command"), "args": n8n_cfg.get("args", [])}
    except Exception:
        pass
    return None


_n8n_config = _load_mcp_config()
_npx_available = shutil.which("npx") is not None

skip_no_n8n = pytest.mark.skipif(
    _n8n_config is None,
    reason="n8n config not found in .mcp.json (need N8N_API_URL + N8N_API_KEY)",
)
skip_no_npx = pytest.mark.skipif(
    not _npx_available,
    reason="npx not found on PATH (required for n8n-mcp stdio transport)",
)

WORKFLOW_NAME = "Renfield Morning Briefing (E2E Test)"


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(scope="module")
def n8n_env():
    """Inject N8N_API_URL and N8N_API_KEY into os.environ from .mcp.json."""
    cfg = _load_mcp_config()
    if not cfg:
        pytest.skip("n8n config not available")

    old_url = os.environ.get("N8N_API_URL")
    old_key = os.environ.get("N8N_API_KEY")

    os.environ["N8N_API_URL"] = cfg["url"]
    os.environ["N8N_API_KEY"] = cfg["key"]

    yield cfg

    # Restore original env
    if old_url is None:
        os.environ.pop("N8N_API_URL", None)
    else:
        os.environ["N8N_API_URL"] = old_url
    if old_key is None:
        os.environ.pop("N8N_API_KEY", None)
    else:
        os.environ["N8N_API_KEY"] = old_key


@pytest.fixture(scope="module")
def morning_briefing_workflow() -> dict:
    """Load the morning briefing workflow JSON from docs/n8n-workflows/."""
    workflow_path = PROJECT_ROOT / "docs" / "n8n-workflows" / "morning-briefing.json"
    if not workflow_path.exists():
        pytest.skip(f"Workflow file not found: {workflow_path}")
    return json.loads(workflow_path.read_text())


@pytest.fixture
async def n8n_mcp_manager(n8n_env):
    """
    Create a real MCPManager connected to n8n via stdio transport.

    Spawns `npx -y n8n-mcp` as a subprocess with real API credentials.
    """
    cfg = n8n_env
    manager = MCPManager()

    # Configure n8n server directly (bypass YAML loading)
    server_config = MCPServerConfig(
        name="n8n",
        transport=MCPTransportType.STDIO,
        command=cfg.get("command", "npx"),
        args=cfg.get("args", ["-y", "n8n-mcp"]),
        enabled=True,
        refresh_interval=300,
    )

    state = MCPServerState(
        config=server_config,
        rate_limiter=TokenBucketRateLimiter(rate_per_minute=60),
        backoff=ExponentialBackoff(),
    )
    manager._servers["n8n"] = state

    # Connect (spawns the stdio subprocess)
    await manager.connect_all()

    if not state.connected:
        error = state.last_error or "unknown error"
        pytest.skip(f"Could not connect to n8n MCP server: {error}")

    yield manager

    # Teardown: shut down MCP sessions
    await manager.shutdown()


@pytest.fixture
async def created_workflow_id(n8n_mcp_manager, morning_briefing_workflow):
    """
    Create the morning briefing workflow in n8n, yield its ID, delete on teardown.

    Ensures idempotency — no artifacts left in n8n after test run.
    """
    workflow = morning_briefing_workflow
    result = await n8n_mcp_manager.execute_tool(
        "mcp.n8n.n8n_create_workflow",
        {
            "name": WORKFLOW_NAME,
            "nodes": workflow["nodes"],
            "connections": workflow["connections"],
            "settings": workflow.get("settings", {}),
        },
    )

    assert result["success"], f"Failed to create workflow: {result.get('message')}"

    # Extract workflow ID from the response
    workflow_id = _extract_workflow_id(result)
    assert workflow_id, f"Could not extract workflow ID from response: {result}"

    yield workflow_id

    # Teardown: delete the workflow
    delete_result = await n8n_mcp_manager.execute_tool(
        "mcp.n8n.n8n_delete_workflow",
        {"id": workflow_id},
    )
    if not delete_result.get("success"):
        # Best-effort cleanup — don't fail the test on cleanup errors
        print(f"WARNING: Failed to delete test workflow {workflow_id}: {delete_result.get('message')}")


def _extract_workflow_id(result: dict) -> str | None:
    """Extract workflow ID from MCP tool result.

    The n8n-mcp server wraps responses as: {"success": true, "data": {"id": "..."}, "message": "..."}
    This JSON is returned as text in the MCP message field.
    """
    for text in _iter_response_texts(result):
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                continue
            # Direct id at top level
            if parsed.get("id"):
                return str(parsed["id"])
            # Nested in data.id (n8n-mcp format)
            nested = parsed.get("data")
            if isinstance(nested, dict) and nested.get("id"):
                return str(nested["id"])
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _iter_response_texts(result: dict):
    """Yield all text strings from an MCP tool result."""
    message = result.get("message", "")
    if message:
        yield message

    raw_data = result.get("data")
    if isinstance(raw_data, list):
        for item in raw_data:
            text = item.get("text", "") if isinstance(item, dict) else ""
            if text:
                yield text


# ============================================================================
# Tests
# ============================================================================

@skip_no_n8n
@skip_no_npx
@pytest.mark.e2e
class TestRenfieldCreatesN8nWorkflow:
    """Real E2E: Renfield creates Morning Briefing workflow in n8n via MCP."""

    async def test_mcp_manager_connects_to_n8n(self, n8n_mcp_manager):
        """Assert n8n MCP server is connected and tools are discovered."""
        manager = n8n_mcp_manager

        # Server must be connected
        state = manager._servers.get("n8n")
        assert state is not None, "n8n server not in manager._servers"
        assert state.connected is True, f"n8n not connected: {state.last_error}"

        # n8n_create_workflow tool must be in the tool index
        assert "mcp.n8n.n8n_create_workflow" in manager._tool_index, (
            f"n8n_create_workflow not found. Available tools: "
            f"{list(manager._tool_index.keys())[:10]}..."
        )

        # Also check for get/delete/list tools we'll use
        for tool_name in ["n8n_get_workflow", "n8n_delete_workflow", "n8n_list_workflows"]:
            assert f"mcp.n8n.{tool_name}" in manager._tool_index, (
                f"{tool_name} not found in tool index"
            )

    async def test_create_morning_briefing_workflow(
        self, n8n_mcp_manager, morning_briefing_workflow, created_workflow_id
    ):
        """Create the Morning Briefing workflow and verify success."""
        # The created_workflow_id fixture already created it and asserted success
        assert created_workflow_id is not None
        assert len(created_workflow_id) > 0
        print(f"\n  Created workflow ID: {created_workflow_id}")

    async def test_verify_workflow_in_n8n(self, n8n_mcp_manager, created_workflow_id):
        """Fetch the created workflow from n8n and verify its contents."""
        result = await n8n_mcp_manager.execute_tool(
            "mcp.n8n.n8n_get_workflow",
            {"id": created_workflow_id},
        )
        assert result["success"], f"Failed to get workflow: {result.get('message')}"

        # Parse the workflow data from the response
        workflow_data = _parse_workflow_response(result)
        assert workflow_data is not None, f"Could not parse workflow from response: {result}"

        # Verify name
        assert workflow_data.get("name") == WORKFLOW_NAME

        # Verify 6 nodes
        nodes = workflow_data.get("nodes", [])
        assert len(nodes) == 6, f"Expected 6 nodes, got {len(nodes)}: {[n.get('name') for n in nodes]}"

        # Verify connections present
        connections = workflow_data.get("connections", {})
        assert len(connections) > 0, "Workflow has no connections"

    async def test_full_path_via_action_executor(self, n8n_mcp_manager, created_workflow_id):
        """
        Prove the full intent -> ActionExecutor -> MCPManager -> n8n path works.

        Uses mcp.n8n.n8n_list_workflows and verifies our test workflow appears.
        """
        executor = ActionExecutor(mcp_manager=n8n_mcp_manager)
        result = await executor.execute({
            "intent": "mcp.n8n.n8n_list_workflows",
            "parameters": {},
            "confidence": 1.0,
        })

        assert result["success"], f"ActionExecutor failed: {result.get('message')}"

        # The response should contain our workflow name somewhere
        message = result.get("message", "")
        assert WORKFLOW_NAME in message or created_workflow_id in message, (
            f"Created workflow not found in list response. "
            f"Looking for '{WORKFLOW_NAME}' or '{created_workflow_id}' in response."
        )

    async def test_workflow_structure_matches_spec(
        self, n8n_mcp_manager, created_workflow_id, morning_briefing_workflow
    ):
        """Verify the created workflow's structure matches the spec."""
        result = await n8n_mcp_manager.execute_tool(
            "mcp.n8n.n8n_get_workflow",
            {"id": created_workflow_id},
        )
        assert result["success"]

        workflow_data = _parse_workflow_response(result)
        assert workflow_data is not None

        nodes = workflow_data.get("nodes", [])
        node_types = {n["name"]: n["type"] for n in nodes}

        # Verify expected node types
        expected_types = {
            "Schedule Trigger": "n8n-nodes-base.scheduleTrigger",
            "Fetch HA States": "n8n-nodes-base.httpRequest",
            "Fetch Weather": "n8n-nodes-base.httpRequest",
            "Merge Data": "n8n-nodes-base.merge",
            "Build Briefing": "n8n-nodes-base.code",
            "POST to Renfield": "n8n-nodes-base.httpRequest",
        }
        for name, expected_type in expected_types.items():
            assert name in node_types, f"Node '{name}' not found in workflow"
            assert node_types[name] == expected_type, (
                f"Node '{name}': expected type '{expected_type}', got '{node_types[name]}'"
            )

        # Verify connection topology
        connections = workflow_data.get("connections", {})

        # Fan-out: Schedule Trigger -> Fetch HA States + Fetch Weather
        trigger_conns = connections.get("Schedule Trigger", {})
        trigger_targets = _get_connection_targets(trigger_conns)
        assert "Fetch HA States" in trigger_targets, "Missing connection: Schedule Trigger -> Fetch HA States"
        assert "Fetch Weather" in trigger_targets, "Missing connection: Schedule Trigger -> Fetch Weather"

        # Fan-in: Fetch HA States + Fetch Weather -> Merge Data
        ha_targets = _get_connection_targets(connections.get("Fetch HA States", {}))
        weather_targets = _get_connection_targets(connections.get("Fetch Weather", {}))
        assert "Merge Data" in ha_targets, "Missing connection: Fetch HA States -> Merge Data"
        assert "Merge Data" in weather_targets, "Missing connection: Fetch Weather -> Merge Data"

        # Merge -> Build Briefing -> POST to Renfield
        merge_targets = _get_connection_targets(connections.get("Merge Data", {}))
        assert "Build Briefing" in merge_targets, "Missing connection: Merge Data -> Build Briefing"

        briefing_targets = _get_connection_targets(connections.get("Build Briefing", {}))
        assert "POST to Renfield" in briefing_targets, "Missing connection: Build Briefing -> POST to Renfield"

        # Verify schedule trigger has 2 intervals (weekdays + weekends)
        trigger_node = next(n for n in nodes if n["name"] == "Schedule Trigger")
        intervals = trigger_node.get("parameters", {}).get("rule", {}).get("interval", [])
        assert len(intervals) == 2, f"Expected 2 schedule intervals, got {len(intervals)}"

    async def test_cleanup_deletes_workflow(self, n8n_mcp_manager, created_workflow_id):
        """Explicitly delete the workflow and verify it's gone."""
        # Delete
        delete_result = await n8n_mcp_manager.execute_tool(
            "mcp.n8n.n8n_delete_workflow",
            {"id": created_workflow_id},
        )
        assert delete_result["success"], f"Delete failed: {delete_result.get('message')}"

        # Verify deletion was acknowledged in the response
        delete_inner = _parse_inner_response(delete_result)
        assert delete_inner.get("success") is True, (
            f"Inner delete response not successful: {delete_inner}"
        )

        # Verify it's gone — get_workflow should return an error or not-found
        get_result = await n8n_mcp_manager.execute_tool(
            "mcp.n8n.n8n_get_workflow",
            {"id": created_workflow_id},
        )
        # The n8n-mcp server wraps errors in {"success": false, "error": "..."}
        # but MCP-level success may still be True. Check the inner response.
        get_inner = _parse_inner_response(get_result)
        is_gone = (
            not get_result["success"]
            or get_inner.get("success") is False
            or "not found" in get_result.get("message", "").lower()
            or "not found" in str(get_inner.get("error", "")).lower()
        )
        assert is_gone, (
            f"Workflow {created_workflow_id} still exists after deletion. "
            f"Response: {get_result.get('message', '')[:200]}"
        )


# ============================================================================
# Helpers
# ============================================================================

def _parse_inner_response(result: dict) -> dict:
    """Parse the n8n-mcp inner JSON response from an MCP tool result.

    Returns the parsed dict, or empty dict if parsing fails.
    """
    for text in _iter_response_texts(result):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            continue
    return {}


def _parse_workflow_response(result: dict) -> dict | None:
    """Parse workflow data from an MCP tool response.

    Handles both direct workflow JSON and n8n-mcp wrapped format:
    {"success": true, "data": {workflow...}}
    """
    for text in _iter_response_texts(result):
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                continue
            # Direct workflow with nodes
            if "nodes" in parsed:
                return parsed
            # Nested in data (n8n-mcp format)
            nested = parsed.get("data")
            if isinstance(nested, dict) and "nodes" in nested:
                return nested
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _get_connection_targets(conn_data: dict) -> set[str]:
    """Extract target node names from a connections entry."""
    targets = set()
    main = conn_data.get("main", [])
    for output_group in main:
        if isinstance(output_group, list):
            for conn in output_group:
                if isinstance(conn, dict) and "node" in conn:
                    targets.add(conn["node"])
    return targets
