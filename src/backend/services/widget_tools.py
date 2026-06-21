"""
Gen-UI widget tools — platform-owned `internal.*` tools that emit typed Lane-A
artifacts (table / list / weather) the chat handler renders inline.

These are the agent-callable counterpart to the smart-home sub-intent producers:
when the user asks for a list or a table, the agent calls `render_list` /
`render_table` with the structured data it already computed (incl. RAG-backed
answers); when the user asks about the weather, it calls `weather_widget`, which
fetches the Open-Meteo MCP and maps the result to a `weather` artifact.

Security boundary is identical to every other artifact: typed JSON → React, no
model HTML/SVG. The structured data comes from the agent as tool args, NEVER
from parsing its free-text — and each artifact is run through
`artifact_service.validate_artifact` (kind allowlist + DoS caps) before it can
reach the wire.

Wiring: each tool returns ``data={"artifacts": [<validated artifact>]}``. The
chat handler already collects every tool result's ``data`` into
``agent_tool_results``; it scans those for an ``artifacts`` key and feeds them to
``_emit_turn_artifacts`` (same validate→emit→persist path as the producers).

Registration is the platform two-step (mirrors knowledge_tool):
  1) ``WIDGET_TOOLS`` is merged in ``agent_tools._register_internal_tools``.
  2) ``action_executor.execute`` dispatches the three intents (weather gets
     ``mcp_manager`` injected).
Plus the role wiring in ``config/agent_roles.yaml`` (``internal_tools``).
"""
from __future__ import annotations

import json
import uuid

from loguru import logger

from services.artifact_service import ArtifactRejected, validate_artifact

# Tool schema shown to the agent. Complex args (columns/rows/items) may arrive
# either as real JSON arrays or as JSON-encoded strings (depending on the model's
# tool-call encoding) — the handlers coerce both.
WIDGET_TOOLS: dict[str, dict] = {
    "internal.render_table": {
        "description": (
            "Render a TABLE widget inline in the chat when the user asks for a "
            "table or for tabular/comparison data. Pass the data you computed — "
            "do NOT also repeat the whole table in prose; give a one-line intro. "
            "columns + rows are arrays (or JSON-array strings)."
        ),
        "parameters": {
            "columns": "Array of column header strings, e.g. [\"Name\",\"Price\"] (required)",
            "rows": "Array of rows; each row an array of cell strings aligned to columns (required)",
            "title": "Optional short table title",
        },
    },
    "internal.render_list": {
        "description": (
            "Render a LIST widget inline in the chat when the user asks for a "
            "list / enumeration of items. Pass the items you computed — do NOT "
            "also repeat the whole list in prose; give a one-line intro. items "
            "is an array (or JSON-array string)."
        ),
        "parameters": {
            "items": "Array of item strings (required)",
            "ordered": "true for a numbered list, false/omit for bullets",
            "title": "Optional short list title",
        },
    },
    "internal.weather_widget": {
        "description": (
            "Show a WEATHER widget (current conditions + a short daily forecast) "
            "for a place. Use it whenever the user asks about the weather or a "
            "forecast. Returns the data so you can add a one-line spoken summary "
            "alongside the widget (don't restate the whole forecast in prose)."
        ),
        "parameters": {
            "location": "City or place name, e.g. \"Berlin\" or a postal code (required)",
            "days": "Forecast days 1-7 (default 5)",
        },
    },
}


def _art_id(prefix: str) -> str:
    return f"art_{prefix}_{uuid.uuid4().hex[:12]}"


def _as_list(value: object) -> list | None:
    """Coerce a tool arg to a list: pass a real list through; JSON-parse a
    JSON-array string; everything else → None (caller rejects)."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def _reject(message: str) -> dict:
    return {"success": False, "message": message, "action_taken": False}


def _ok(artifact: dict, message: str) -> dict:
    # action_taken stays False — rendering a widget is presentation, not a
    # state-changing action (mirrors the read-only sub-intent producers).
    return {
        "success": True,
        "message": message,
        "action_taken": False,
        "data": {"artifacts": [artifact]},
    }


async def render_table(parameters: dict) -> dict:
    """Validate the agent's columns/rows into a `table` artifact."""
    columns = _as_list(parameters.get("columns"))
    rows = _as_list(parameters.get("rows"))
    if columns is None or rows is None:
        return _reject("render_table needs `columns` (array) and `rows` (array of arrays).")
    artifact = {
        "id": _art_id("table"),
        "kind": "table",
        "data": {"columns": columns, "rows": rows},
    }
    title = parameters.get("title")
    if isinstance(title, str) and title.strip():
        artifact["title"] = title.strip()
    try:
        cleaned = validate_artifact(artifact)
    except ArtifactRejected as e:
        logger.warning(f"render_table rejected: {e}")
        return _reject(f"The table data was invalid: {e}")
    return _ok(cleaned, f"Rendered a table ({len(cleaned['data']['rows'])} rows).")


async def render_list(parameters: dict) -> dict:
    """Validate the agent's items into a `list` artifact."""
    items = _as_list(parameters.get("items"))
    if items is None:
        return _reject("render_list needs `items` (array of strings).")
    data: dict = {"items": items}
    ordered = parameters.get("ordered")
    if isinstance(ordered, bool):
        data["ordered"] = ordered
    elif isinstance(ordered, str):
        data["ordered"] = ordered.strip().lower() in ("true", "1", "yes")
    artifact: dict = {"id": _art_id("list"), "kind": "list", "data": data}
    title = parameters.get("title")
    if isinstance(title, str) and title.strip():
        artifact["title"] = title.strip()
    try:
        cleaned = validate_artifact(artifact)
    except ArtifactRejected as e:
        logger.warning(f"render_list rejected: {e}")
        return _reject(f"The list data was invalid: {e}")
    return _ok(cleaned, f"Rendered a list ({len(cleaned['data']['items'])} items).")


def _extract_mcp_payload(res: dict) -> dict:
    """Pull the JSON dict payload out of an MCPManager.execute_tool result.

    The wrapper returns ``data`` as a LIST of content blocks
    (`[{"type":"text","text":"<json>"}]`), not the deserialized object — so parse
    it from the text block, falling back to a flat dict `data` or `message`.
    Mirrors the ha_glue `_extract_mcp_json` helper (kept local so platform code
    has no ha_glue import)."""
    data = res.get("data")
    if isinstance(data, dict):
        return data
    raw_text = ""
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("type") == "text":
                raw_text = item.get("text", "")
                break
    if not raw_text:
        raw_text = res.get("message", "") or ""
    try:
        parsed = json.loads(raw_text)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_weather_artifact(raw: dict) -> dict | None:
    """Map an Open-Meteo `get_weather` payload to a `weather` artifact, defensively.

    get_weather shape (see renfield_mcp_weather):
      {location:{name,country,...}, current:{temperature,weather_code,
       weather_description,feels_like,humidity,wind_speed,...},
       daily:[{date,temp_max,temp_min,weather_code,weather_description,
       precipitation_probability,...}]}
    Returns None if there's no current block to show (caller falls back to prose).
    """
    cur = raw.get("current")
    if not isinstance(cur, dict) or cur.get("temperature") is None:
        return None
    daily = raw.get("daily") if isinstance(raw.get("daily"), list) else []

    current: dict = {
        "temp": cur.get("temperature"),
        "unit": "°C",  # the tool requests celsius from the MCP
        "code": cur.get("weather_code", 0),
        "condition": cur.get("weather_description", ""),
    }
    if cur.get("feels_like") is not None:
        current["feelsLike"] = cur["feels_like"]
    if cur.get("humidity") is not None:
        current["humidity"] = cur["humidity"]
    if cur.get("wind_speed") is not None:
        current["windSpeed"] = cur["wind_speed"]
    # Today's high/low come from daily[0].
    if daily and isinstance(daily[0], dict):
        if daily[0].get("temp_max") is not None:
            current["high"] = daily[0]["temp_max"]
        if daily[0].get("temp_min") is not None:
            current["low"] = daily[0]["temp_min"]

    forecast: list[dict] = []
    for d in daily[1:8]:  # skip today; next up to 7 days
        if not isinstance(d, dict):
            continue
        if d.get("temp_max") is None or d.get("temp_min") is None:
            continue
        entry: dict = {
            "date": d.get("date", ""),
            "code": d.get("weather_code", 0),
            "high": d["temp_max"],
            "low": d["temp_min"],
        }
        if d.get("weather_description"):
            entry["condition"] = d["weather_description"]
        if d.get("precipitation_probability") is not None:
            entry["precipChance"] = d["precipitation_probability"]
        forecast.append(entry)

    loc = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    artifact: dict = {
        "id": _art_id("weather"),
        "kind": "weather",
        "title": loc.get("name") or "Wetter",
        "data": {
            "location": loc.get("name", ""),
            "current": current,
            **({"forecast": forecast} if forecast else {}),
        },
    }
    return artifact


async def weather_widget(parameters: dict, *, mcp_manager) -> dict:
    """Fetch weather via the Open-Meteo MCP and emit a `weather` artifact.

    Returns the raw weather data to the agent (for a one-line spoken summary)
    plus the artifact. Falls back to a plain error/result when the MCP is
    unavailable or the location can't be resolved.
    """
    location = (parameters.get("location") or "").strip()
    if not location:
        return _reject(
            "weather_widget needs a `location` (city or place). Ask the user, or "
            "use their home city from context."
        )
    if mcp_manager is None:
        return _reject("Weather service is not available on this deploy.")

    try:
        days = int(parameters.get("days", 5))
    except (ValueError, TypeError):
        days = 5
    days = max(1, min(7, days))

    try:
        res = await mcp_manager.execute_tool(
            "mcp.weather.get_weather",
            {"location": location, "days": days, "temperature_unit": "celsius"},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"weather_widget: MCP call failed: {e}")
        return _reject(f"Could not fetch the weather: {e}")

    if not res.get("success", True):
        return _reject(f"Could not fetch the weather: {res.get('message', 'unknown error')}")

    raw = _extract_mcp_payload(res)
    if raw.get("error"):
        # The MCP returns {"error": ...} for an unresolvable location.
        return _reject(str(raw["error"]))

    artifact = _build_weather_artifact(raw)
    if artifact is None:
        # No current block — hand the raw data back so the agent can still answer.
        return {
            "success": True,
            "message": f"Weather data for {location} (no current conditions to render).",
            "action_taken": False,
            "data": {"weather": raw},
        }

    try:
        cleaned = validate_artifact(artifact)
    except ArtifactRejected as e:
        logger.warning(f"weather_widget artifact rejected: {e}")
        # Still return the raw data so the agent can answer in prose.
        return {
            "success": True,
            "message": f"Weather data for {location}.",
            "action_taken": False,
            "data": {"weather": raw},
        }

    _loc = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    loc_name = _loc.get("name", location)
    return {
        "success": True,
        "message": f"Showing the weather for {loc_name}.",
        "action_taken": False,
        # Both the artifact (rendered) AND the raw data (for the agent's prose).
        "data": {"artifacts": [cleaned], "weather": raw},
    }
