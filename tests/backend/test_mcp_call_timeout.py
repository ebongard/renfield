"""Per-server MCP tool-call timeout.

The failure this encodes, 2026-09-14: `mcp.scanner.scan_document` hit the global
30s `mcp_call_timeout` and the agent told the user the scan had FAILED — the scan
finished and was ingested 80ms later. A tool that feeds a paper stack cannot fit
a timeout sized for a weather lookup, so a server may carry its own.
"""
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize(
    "raw",
    [None, "", "abc", 0, -5, 0.5, 99999, "1", True],
    ids=["absent", "blank", "unparseable", "zero", "negative", "below-min",
         "above-max", "boolean-like-string", "boolean"],
)
def test_unusable_value_falls_back_to_global(raw):
    """A typo costs only the override, never the server."""
    from services.mcp_client import _parse_call_timeout

    assert _parse_call_timeout(raw) is None


def test_numeric_and_string_values_parse():
    from services.mcp_client import _parse_call_timeout

    assert _parse_call_timeout(600) == 600.0
    assert _parse_call_timeout("120") == 120.0
    assert _parse_call_timeout("2.5") == 2.5


def test_range_bounds_are_inclusive():
    from services.mcp_client import _parse_call_timeout

    assert _parse_call_timeout(1) == 1.0
    assert _parse_call_timeout(3600) == 3600.0


def test_transport_read_timeout_outlasts_the_call():
    """The SDK HTTP transports default to a 300s read timeout. A longer call
    would die at the transport as an opaque error, so the transport must always
    outlast the call — and never drop below the SDK default."""
    from services.mcp_client import MCPServerConfig, _transport_read_timeout

    assert _transport_read_timeout(MCPServerConfig(name="s")) == 300.0
    assert _transport_read_timeout(MCPServerConfig(name="s", call_timeout=60.0)) == 300.0
    long_call = MCPServerConfig(name="s", call_timeout=600.0)
    assert _transport_read_timeout(long_call) > 600.0


def test_env_substitution_default_applies(monkeypatch):
    from services.mcp_client import _parse_call_timeout

    monkeypatch.delenv("EXAMPLE_MCP_CALL_TIMEOUT", raising=False)
    assert _parse_call_timeout("${EXAMPLE_MCP_CALL_TIMEOUT:-600}") == 600.0

    monkeypatch.setenv("EXAMPLE_MCP_CALL_TIMEOUT", "900")
    assert _parse_call_timeout("${EXAMPLE_MCP_CALL_TIMEOUT:-600}") == 900.0


def test_server_override_wins_over_global(monkeypatch):
    from services.mcp_client import _server_call_timeout
    from utils.config import settings

    monkeypatch.setattr(settings, "mcp_call_timeout", 30.0)
    assert _server_call_timeout(SimpleNamespace(config=SimpleNamespace(call_timeout=600.0))) == 600.0
    assert _server_call_timeout(SimpleNamespace(config=SimpleNamespace(call_timeout=None))) == 30.0
    assert _server_call_timeout(None) == 30.0


def test_mapping_parses_per_tool_and_drops_bad_entries():
    from services.mcp_client import _parse_call_timeout

    parsed = _parse_call_timeout({"scan_document": "600", "default": 20, "broken": "abc"})
    assert parsed == {"scan_document": 600.0, "default": 20.0}
    assert _parse_call_timeout({"broken": "abc"}) is None


def test_per_tool_entry_beats_default_beats_global(monkeypatch):
    """One long tool must not stretch its siblings: a status query keeps a short
    timeout even when the scan tool on the same server gets minutes."""
    from services.mcp_client import _server_call_timeout
    from utils.config import settings

    monkeypatch.setattr(settings, "mcp_call_timeout", 30.0)
    state = SimpleNamespace(config=SimpleNamespace(
        call_timeout={"scan_document": 600.0, "default": 15.0}))
    assert _server_call_timeout(state, "scan_document") == 600.0
    assert _server_call_timeout(state, "scanner_status") == 15.0

    no_default = SimpleNamespace(config=SimpleNamespace(call_timeout={"scan_document": 600.0}))
    assert _server_call_timeout(no_default, "scanner_status") == 30.0


def test_transport_outlasts_the_longest_tool():
    from services.mcp_client import MCPServerConfig, _transport_read_timeout

    config = MCPServerConfig(name="s", call_timeout={"quick": 10.0, "slow": 900.0})
    assert _transport_read_timeout(config) > 900.0


def _slow_manager(server_call_timeout):
    import asyncio
    from unittest.mock import AsyncMock

    from services.mcp_client import MCPManager, MCPServerConfig, MCPServerState, MCPToolInfo

    manager = MCPManager()
    manager._tool_index["mcp.srv.slow"] = MCPToolInfo("srv", "slow", "mcp.srv.slow", "Slow tool")

    async def slow_call(*args, **kwargs):
        await asyncio.sleep(0.2)

    session = AsyncMock()
    session.call_tool = slow_call
    manager._servers["srv"] = MCPServerState(
        config=MCPServerConfig(name="srv", call_timeout=server_call_timeout),
        connected=True,
        session=session,
    )
    return manager


@pytest.mark.asyncio
async def test_execute_tool_applies_the_server_override():
    """The wiring, not just the helper: a short server override must cut a call
    the (long) global timeout would have let run."""
    from unittest.mock import patch

    manager = _slow_manager(server_call_timeout=0.01)
    with patch("services.mcp_client.settings") as mock_settings:
        mock_settings.mcp_call_timeout = 30.0
        result = await manager.execute_tool("mcp.srv.slow", {})

    assert result["success"] is False
    assert "Timeout" in result["message"]


@pytest.mark.asyncio
async def test_per_call_timeout_still_wins_over_the_server_override():
    """Precedence: per-call > server > global. A deliberately-blocking poll tool
    passing its own call_timeout must not be cut by a shorter server override."""
    from unittest.mock import patch

    manager = _slow_manager(server_call_timeout=0.01)
    with patch("services.mcp_client.settings") as mock_settings:
        mock_settings.mcp_call_timeout = 0.01
        result = await manager.execute_tool("mcp.srv.slow", {}, call_timeout=5.0)

    assert "Timeout" not in (result.get("message") or "")


def test_scanner_timeouts_follow_which_tools_still_work_inside_the_call():
    """`scan_document` only starts a job — a long timeout there would be the old
    shape creeping back. `route_scan` and `retry_pending_scans` still OCR and push
    inside the call and must not fall back to 30s (review finding: they would
    report FAILED for documents that were filed)."""
    from pathlib import Path

    import yaml

    from services.mcp_client import _parse_call_timeout, _server_call_timeout
    from utils.config import settings

    root = Path(__file__).resolve().parents[2]
    entries = yaml.safe_load((root / "config" / "mcp_servers.yaml").read_text())["servers"]
    scanner = next(e for e in entries if e["name"] == "scanner")
    state = SimpleNamespace(config=SimpleNamespace(
        call_timeout=_parse_call_timeout(scanner.get("call_timeout"))))

    assert _server_call_timeout(state, "scan_document") == settings.mcp_call_timeout
    assert _server_call_timeout(state, "scanner_status") == settings.mcp_call_timeout
    assert _server_call_timeout(state, "route_scan") >= 300
    assert _server_call_timeout(state, "retry_pending_scans") >= 300
