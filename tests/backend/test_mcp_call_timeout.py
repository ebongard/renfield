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


def test_env_substitution_default_applies(monkeypatch):
    from services.mcp_client import _parse_call_timeout

    monkeypatch.delenv("SCANNER_MCP_CALL_TIMEOUT", raising=False)
    assert _parse_call_timeout("${SCANNER_MCP_CALL_TIMEOUT:-600}") == 600.0

    monkeypatch.setenv("SCANNER_MCP_CALL_TIMEOUT", "900")
    assert _parse_call_timeout("${SCANNER_MCP_CALL_TIMEOUT:-600}") == 900.0


def test_server_override_wins_over_global(monkeypatch):
    from services.mcp_client import _server_call_timeout
    from utils.config import settings

    monkeypatch.setattr(settings, "mcp_call_timeout", 30.0)
    assert _server_call_timeout(SimpleNamespace(config=SimpleNamespace(call_timeout=600.0))) == 600.0
    assert _server_call_timeout(SimpleNamespace(config=SimpleNamespace(call_timeout=None))) == 30.0
    assert _server_call_timeout(None) == 30.0


def test_scanner_stanza_carries_a_long_timeout(monkeypatch):
    """The shipped YAML must actually give the scanner its override — the parser
    alone fixes nothing if the stanza never sets it."""
    from pathlib import Path

    import yaml

    from services.mcp_client import _parse_call_timeout

    monkeypatch.delenv("SCANNER_MCP_CALL_TIMEOUT", raising=False)
    root = Path(__file__).resolve().parents[2]
    entries = yaml.safe_load((root / "config" / "mcp_servers.yaml").read_text())["servers"]
    scanner = next(e for e in entries if e["name"] == "scanner")
    assert _parse_call_timeout(scanner.get("call_timeout")) >= 300
