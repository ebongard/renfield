"""
Context Variable Extraction -- Extracts structured entities from MCP tool results.

Uses YAML-configurable rules to extract entities from tool results (pure JSON
path resolution, no LLM cost). Extracted variables are persisted as conversation
context so follow-up queries like "Zeig mehr Details" know what was last queried.
"""

import json
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

_extraction_rules: dict[str, dict[str, list[dict]]] = {}


def load_extraction_config(config_path: str | None = None) -> None:
    """Load extraction rules from YAML config."""
    global _extraction_rules

    if config_path is None:
        config_path = str(
            Path(__file__).resolve().parent.parent / "config" / "context_extraction.yaml"
        )

    path = Path(config_path)
    if not path.exists():
        logger.debug(f"Context extraction config not found: {path}")
        return

    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        _extraction_rules = raw
        rule_count = sum(len(tools) for tools in raw.values())
        logger.info(f"Context extraction loaded: {rule_count} tool rules from {path}")
    except Exception:
        logger.opt(exception=True).warning(
            f"Failed to load context extraction config: {path}"
        )


def extract_context_vars(tool_name: str, result: dict) -> dict[str, Any]:
    """Extract structured entities from an MCP tool result.

    Args:
        tool_name: Namespaced tool name (e.g. "mcp.paperless.search_documents")
        result: The MCP result dict {"success": bool, "message": str, "data": ...}

    Returns:
        Dict of context variables to merge into conversation state.
        Empty dict if no rules match or extraction fails.
    """
    if not result.get("success"):
        return {}

    # Resolve server + tool from namespaced name
    if not tool_name.startswith("mcp."):
        return {}
    parts = tool_name.split(".", 2)
    if len(parts) < 3:
        return {}
    server, tool = parts[1], parts[2]

    server_rules = _extraction_rules.get(server)
    if not server_rules:
        return {}
    rules = server_rules.get(tool)
    if not rules:
        return {}

    # Parse the result message as JSON
    message = result.get("message", "")
    try:
        data = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return {}

    # Apply extraction rules
    extracted: dict[str, Any] = {}
    for rule in rules:
        key = rule.get("key")
        path = rule.get("path")
        if not key or not path:
            continue

        value = _resolve_path(data, path)
        if value is not None:
            extracted[key] = value

    if extracted:
        logger.debug(
            f"Context vars extracted from {tool_name}: "
            f"{list(extracted.keys())}"
        )

    return extracted


def _resolve_path(data: Any, path: str) -> Any:
    """Resolve a JSON path expression against data.

    Supported syntax:
    - "field"           → data["field"]
    - "field.nested"    → data["field"]["nested"]
    - "[0].field"       → data[0]["field"]
    - "[].field"        → [item["field"] for item in data]
    - "__length__"      → len(data)
    """
    if path == "__length__":
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            return len(data)
        return None

    parts = path.split(".")
    return _resolve_parts(data, parts)


def _resolve_parts(data: Any, parts: list[str]) -> Any:
    """Recursively resolve path parts against data."""
    if not parts:
        return data

    part = parts[0]
    remaining = parts[1:]

    # Array index: [0], [1], etc.
    if part.startswith("[") and part.endswith("]"):
        idx_str = part[1:-1]
        if not isinstance(data, list):
            return None

        if idx_str == "":
            # Collect from all items: [].field
            results = []
            for item in data:
                value = _resolve_parts(item, remaining)
                if value is not None:
                    results.append(value)
            return results if results else None

        try:
            idx = int(idx_str)
            if idx < len(data):
                return _resolve_parts(data[idx], remaining)
        except ValueError:
            pass
        return None

    # Regular field access
    if isinstance(data, dict) and part in data:
        return _resolve_parts(data[part], remaining)

    return None
