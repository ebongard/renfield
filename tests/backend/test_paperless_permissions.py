"""Paperless MCP read/write permission split (login audit #1116, Batch B, #2).

The paperless stanza (config/mcp_servers.yaml) now maps read tools to
mcp.paperless.read and mutations (update_document, create_*, delete_document,
upload_document) to mcp.paperless.write, so a read-only role can no longer
drive a Paperless write. BACKWARD-COMPATIBLE: the broad `mcp.paperless` server
grant still covers both, so existing search flows don't break on deploy — the
restriction bites once a role is tightened to `mcp.paperless.read`.
"""
from models.permissions import has_mcp_permission


def test_paperless_read_write_separation():
    # read-only grant: reads yes, writes NO
    assert has_mcp_permission(["mcp.paperless.read"], "mcp.paperless.read") is True
    assert has_mcp_permission(["mcp.paperless.read"], "mcp.paperless.write") is False

    # write grant: writes yes, reads NO (separation is symmetric)
    assert has_mcp_permission(["mcp.paperless.write"], "mcp.paperless.write") is True
    assert has_mcp_permission(["mcp.paperless.write"], "mcp.paperless.read") is False

    # broad server grant still covers BOTH → no breakage for existing roles
    assert has_mcp_permission(["mcp.paperless"], "mcp.paperless.read") is True
    assert has_mcp_permission(["mcp.paperless"], "mcp.paperless.write") is True

    # mcp.* wildcard covers both
    assert has_mcp_permission(["mcp.*"], "mcp.paperless.write") is True


def test_paperless_stanza_maps_tools():
    """The committed stanza actually carries the split (guards against someone
    dropping tool_permissions)."""
    import yaml
    from pathlib import Path

    import pytest
    candidates = [
        Path("config/mcp_servers.yaml"),
        Path("/app/config/mcp_servers.yaml"),
        Path(__file__).resolve().parents[2] / "config" / "mcp_servers.yaml",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        pytest.skip("mcp_servers.yaml not reachable in this test env")
    cfg = yaml.safe_load(path.read_text())
    servers = cfg.get("servers") or cfg.get("mcpServers") or []
    paperless = next(s for s in servers if s.get("name") == "paperless")
    tp = paperless.get("tool_permissions", {})
    assert tp.get("update_document") == "mcp.paperless.write"
    assert tp.get("search_documents") == "mcp.paperless.read"
    assert "mcp.paperless.write" in (paperless.get("permissions") or [])
    # EVERY mutation must be mapped to write — an unmapped mutation falls back to
    # the server-level [read, write] and stays drivable by a read-only role.
    for mutation in (
        "update_document", "reprocess_document", "upload_document",
        "create_correspondent", "create_document_type", "create_storage_path",
        "create_tag", "delete_document",
    ):
        assert tp.get(mutation) == "mcp.paperless.write", f"{mutation} not write-gated"
