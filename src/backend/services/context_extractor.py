"""
Context Variable Extraction -- Extracts structured entities from MCP tool results.

Uses YAML-configurable rules to extract entities from tool results (pure JSON
path resolution, no LLM cost). The agent collects them per turn
(``AgentService.run(context_vars_out=...)``); the caller persists them into the
conversation after the turn was saved
(``ConversationService.save_turn_context_vars``), and the next turn renders
them into the agent prompt (``render_context_vars_text`` → ``## Conversation
Context``), so follow-up queries like "Zeig mehr Details" know what was last
queried.

The values come from tool results — untrusted text that re-enters later
prompts. ``bound_context_vars`` is the gate on the way into storage (key shape,
scalar values, whitespace collapsed so a value cannot forge a prompt line,
per-value and per-turn size caps); ``merge_context_vars`` caps how many public
keys a conversation keeps; the prompt builder additionally runs
``neutralize_delimiters`` over the rendered block.
"""

import json
import re
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

_extraction_rules: dict[str, dict[str, list[dict]]] = {}

#: A string value longer than this is cut. Measured against the shipped rules
#: (config/context_extraction.yaml): every rule yields an id, a count or ONE
#: title/name/subject/sender — 200 characters hold any real one of them.
MAX_VALUE_CHARS = 200
#: A list value (a ``[].field`` rule) keeps at most this many items.
MAX_LIST_ITEMS = 10
#: Upper bound of one turn's variables, serialized (keys + JSON values). The
#: shipped rules yield at most 11 keys; even all of them at the value cap stay
#: near this bound, so it only bites on a plugin that returns more.
MAX_TURN_CHARS = 2000
#: Public (non-``_``) keys a conversation keeps. The oldest-updated key is
#: dropped first. Keys starting with ``_`` are internal state
#: (``_active_domain``) and neither count nor get evicted.
MAX_STORED_KEYS = 16

# A public key: starts with a letter, so an extracted or plugin key can never
# shadow internal state (``_active_domain``) or carry prompt syntax.
_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def _bound_scalar(value: Any) -> Any:
    """One scalar, bounded; ``None`` for anything that is not a scalar."""
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        # Collapse every whitespace run (newlines included): a value is one
        # line in the prompt block and must not be able to start another.
        collapsed = " ".join(value.split())
        return collapsed[:MAX_VALUE_CHARS] or None
    return None


def _bound_value(value: Any) -> Any:
    if isinstance(value, list):
        items = [b for b in (_bound_scalar(v) for v in value[:MAX_LIST_ITEMS]) if b is not None]
        return items or None
    return _bound_scalar(value)


def bound_context_vars(raw: dict[str, Any]) -> dict[str, Any]:
    """Gate a turn's extracted variables before they are stored.

    Drops keys that are not public identifiers (see ``_KEY_RE``); collapses
    whitespace and caps string length, list length and the turn's total size
    (``MAX_TURN_CHARS``, first keys win; a key past the cap is cleared). A
    value that is ``None`` or not a
    scalar / list of scalars becomes ``None`` — the delete marker of
    ``merge_context_vars``: the current item has no usable value for that
    key, so an older item's value must not stand in for it.
    """
    bounded: dict[str, Any] = {}
    used = 0
    for key, value in raw.items():
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            continue
        clean = _bound_value(value)
        if clean is None:
            bounded[key] = None
            continue
        size = len(key) + len(json.dumps(clean, ensure_ascii=False))
        if used + size > MAX_TURN_CHARS:
            # Cleared, not skipped: skipping would leave the OLDER item's value
            # under this key next to the new item's other keys.
            logger.debug(f"Context var {key!r} cleared: turn size cap reached")
            bounded[key] = None
            continue
        bounded[key] = clean
        used += size
    return bounded


def merge_context_vars(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Merge ``update`` into ``current`` per key (a ``None`` value removes it).

    An updated key moves to the end, so insertion order is recency order;
    beyond ``MAX_STORED_KEYS`` public keys the oldest are dropped. Internal
    ``_`` keys are kept as they are.
    """
    merged = dict(current)
    for key, value in update.items():
        merged.pop(key, None)
        if value is not None:
            merged[key] = value
    public = [k for k in merged if not k.startswith("_")]
    for key in public[: max(0, len(public) - MAX_STORED_KEYS)]:
        del merged[key]
    return merged


#: Where the per-writer variables live inside ``conversations.context_vars``
#: (#1568): ``{"_per_writer": {"<user id>": {key: value, ...}}}``. Every other
#: top-level key is the LEGACY layer -- written before writers were recorded,
#: or under auth-off, which keeps writing it exactly as before. An ``_`` key, so
#: no extracted or plugin key can reach it (``_KEY_RE``), the plain merge keeps
#: it untouched and the renderer skips it.
WRITERS_KEY = "_per_writer"


def legacy_context_vars(stored: dict[str, Any] | None) -> dict[str, Any]:
    """The variables that name no writer (every key but ``WRITERS_KEY``)."""
    return {k: v for k, v in (stored or {}).items() if k != WRITERS_KEY}


def _buckets(stored: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    raw = (stored or {}).get(WRITERS_KEY)
    if not isinstance(raw, dict):
        return {}
    return {k: dict(v) for k, v in raw.items() if isinstance(v, dict)}


def writer_context_vars(stored: dict[str, Any] | None, writer_id: int) -> dict[str, Any]:
    """The variables ``writer_id`` wrote into this conversation."""
    return _buckets(stored).get(str(int(writer_id)), {})


def merge_writer_context_vars(
    stored: dict[str, Any] | None,
    writer_id: int,
    update: dict[str, Any],
) -> dict[str, Any]:
    """Merge one writer's ``update`` into THEIR bucket only (#1568).

    Another member's bucket is never touched: one member's turn -- its values,
    its ``None`` delete markers, the key cap -- cannot erase what the next
    member's load would see. The legacy layer (values written before the
    writer was recorded) is kept as it is and never folded into a bucket:
    nobody can say whose it is, so under auth it reaches no prompt at all.
    """
    stored = dict(stored or {})
    buckets = _buckets(stored)
    key = str(int(writer_id))
    merged = merge_context_vars(buckets.get(key, {}), update)
    if merged:
        buckets[key] = merged
    else:
        buckets.pop(key, None)
    result = legacy_context_vars(stored)
    if buckets:
        result[WRITERS_KEY] = buckets
    return result


def handoff_context_vars(
    source: dict[str, Any] | None, target: dict[str, Any] | None,
) -> dict[str, Any]:
    """The target room's variables after a handoff from ``source`` (#1568).

    The legacy layer is the source's, as the handoff always copied it; the
    per-writer buckets travel with their writer -- a source bucket replaces the
    same writer's bucket in the target, every other target bucket stays. With
    no buckets on either side this IS ``source`` (auth-off unchanged).
    """
    result = legacy_context_vars(source)
    buckets = {**_buckets(target), **_buckets(source)}
    if buckets:
        result[WRITERS_KEY] = buckets
    return result


def render_context_vars_text(context_vars: dict[str, Any] | None) -> str:
    """The prompt block for the agent: one ``key: value`` line per public key."""
    if not context_vars:
        return ""
    return "\n".join(
        f"{k}: {v}" for k, v in context_vars.items() if not k.startswith("_")
    )


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
        Dict of context variables to merge into conversation state. Once a
        tool's rule set applies (successful result, parsable JSON), EVERY key
        of that rule set is returned: a path that does not resolve yields
        ``None``, which the merge reads as "delete". The keys of one rule set
        describe ONE item — without this, a second search whose hit has no
        correspondent kept the first hit's correspondent next to the second
        hit's title, and a search with zero hits kept the old item as "last".
        Empty dict if no rules match or the result cannot be read (then
        nothing is known about the current item, so nothing is cleared).
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

        extracted[key] = _resolve_path(data, path)

    logger.debug(
        f"Context vars extracted from {tool_name}: "
        f"{sorted(k for k, v in extracted.items() if v is not None)} "
        f"(cleared: {sorted(k for k, v in extracted.items() if v is None)})"
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
