"""
MCP Response Compaction -- Field-level filtering for large MCP tool responses.

Uses YAML-based whitelists per tool to keep only essential fields,
dramatically reducing token consumption in the agent prompt.

Typical reduction: 50KB -> 2-5KB for tools like Home Assistant get_states
or Paperless search_documents.
"""

import json
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

_compact_rules: dict[str, dict[str, list[str]]] = {}


def load_compact_config(config_path: str = "config/mcp_compact.yaml") -> None:
    """Load field whitelist rules from YAML config.

    Format:
        server_name:
          tool_name:
            - field_a
            - field_b.nested
            - items[].name
    """
    global _compact_rules

    path = Path(config_path)
    if not path.exists():
        logger.debug(f"MCP compact config not found: {path}")
        return

    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        _compact_rules = raw
        tool_count = sum(len(tools) for tools in raw.values())
        logger.info(f"MCP compaction loaded: {tool_count} tool rules from {path}")
    except Exception:
        logger.opt(exception=True).warning(f"Failed to load MCP compact config: {path}")


def compact_mcp_result(tool_name: str, result: dict) -> dict:
    """Apply field-level compaction to an MCP tool result.

    Args:
        tool_name: Namespaced tool name (e.g. "mcp.homeassistant.get_states")
        result: The MCP result dict {"success": bool, "message": str, "data": ...}

    Returns:
        Modified result with compacted message/data. If no rules match,
        returns result unchanged.
    """
    resolved = _resolve_tool_name(tool_name)
    if resolved is None:
        return result

    server, tool = resolved
    server_rules = _compact_rules.get(server)
    if not server_rules:
        return result

    field_paths = server_rules.get(tool)
    if not field_paths:
        return result

    # Compact the message field (typically JSON)
    message = result.get("message", "")
    if message:
        try:
            parsed = json.loads(message)
            compacted = _extract_fields(parsed, field_paths)
            result = {**result, "message": json.dumps(compacted, ensure_ascii=False)}
        except (json.JSONDecodeError, TypeError):
            pass  # Not JSON, leave as-is

    # Compact the data field if present
    data = result.get("data")
    if data and isinstance(data, list):
        compacted_data = []
        for item in data:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    parsed = json.loads(item["text"])
                    compacted = _extract_fields(parsed, field_paths)
                    compacted_data.append({**item, "text": json.dumps(compacted, ensure_ascii=False)})
                except (json.JSONDecodeError, TypeError):
                    compacted_data.append(item)
            else:
                compacted_data.append(item)
        result = {**result, "data": compacted_data}

    return result


def _resolve_tool_name(namespaced: str) -> tuple[str, str] | None:
    """Parse 'mcp.server.tool' into (server_name, tool_name).

    Returns None for non-MCP tools or malformed names.
    """
    if not namespaced.startswith("mcp."):
        return None
    parts = namespaced.split(".", 2)
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


def _extract_fields(data: Any, field_paths: list[str]) -> Any:
    """Extract only whitelisted fields from nested data structures."""
    if isinstance(data, list):
        return [_extract_fields(item, field_paths) for item in data]

    if not isinstance(data, dict):
        return data

    # Group paths by their first key to handle arrays correctly.
    # e.g. ["phases[].name", "phases[].tasks[].status", "id"]
    # -> {"phases[]": ["name", "tasks[].status"], "id": []}
    groups: dict[str, list[str]] = {}
    for path in field_paths:
        dot_idx = path.find(".")
        if dot_idx == -1:
            groups.setdefault(path, [])
        else:
            first = path[:dot_idx]
            rest = path[dot_idx + 1:]
            groups.setdefault(first, []).append(rest)

    result: dict[str, Any] = {}
    for key_spec, sub_paths in groups.items():
        is_array = key_spec.endswith("[]")
        key = key_spec[:-2] if is_array else key_spec

        if key not in data:
            continue

        value = data[key]

        if not sub_paths:
            # Leaf field — copy directly
            result[key] = value
        elif is_array and isinstance(value, list):
            # Array traversal — apply sub_paths to each element
            result[key] = [_extract_fields(item, sub_paths) for item in value]
        elif isinstance(value, dict):
            # Nested object — recurse with sub_paths
            result[key] = _extract_fields(value, sub_paths)

    return result
