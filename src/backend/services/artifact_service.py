"""
Chat artifacts — Lane A backend (typed table / list / keyvalue / chart).

Renfield renders generated structured data (a weekly plan, a shopping list, a
smart-home status table, a small chart) inline in a chat turn as **typed JSON →
real React components**. There is no model HTML and no model SVG anywhere in the
path — React's escape boundary on the client is the entire security story.

This module is the BACKEND half. Per the locked design
(`docs/design/chat-artifacts-sandbox.md` §8 decision 3) the backend validates
**only the DoS gate**: the `kind` allowlist + the size / row / series / point
caps. The frontend `artifactSchema.ts` (zod) is the authoritative *shape*
validator and is what renders; a shape mismatch there falls back to an escaped
code block. This is an intentional separation of concern (backend = caps,
frontend = shape), not duplicated validation.

Contract (mirrors the existing `card` WS frame):

    {
      "type": "artifact",
      "artifact": {
        "id": "art_...",          # stable id — idempotent re-emit / streaming patch key
        "kind": "table|list|keyvalue|chart",
        "title": "...",           # optional plain text
        "data": { ... },          # per-kind, see _validate_<kind>
        "partial": false          # true while streaming (frontend appends same-id frames)
      },
      "replace_text": "..."        # optional, same semantics as the card frame
    }

Persistence: validated artifacts ride in ``message_metadata["artifacts"]`` as an
**array keyed by id** (§8 decision 2 — a turn may carry a table AND a chart), so
a history reload rehydrates them via ``historyToUiMessage``. Artifacts are NOT
atoms: no ``circle_tier`` / ``atom_id``, no ``circle_sql`` (same reasoning as
message search — messages aren't atoms).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# --- The kind allowlist (Lane A only; html/svg are Lane B and rejected here) ---
ALLOWED_KINDS: frozenset[str] = frozenset(
    {"table", "list", "keyvalue", "chart", "weather", "device_control", "presence_map"}
)

# --- DoS caps (the backend's ONLY validation responsibility) -----------------
# Generous enough for the motivated use cases (weekly plan ~7 rows, shopping
# list, smart-home status, a small bar/line chart) but a hard ceiling so a
# prompt-injected "emit a 10k-row table" payload never reaches the client.
MAX_TABLE_ROWS = 200            # rows in a table
MAX_TABLE_COLUMNS = 20          # columns in a table
MAX_LIST_ITEMS = 200            # items in a list
MAX_KEYVALUE_PAIRS = 100        # key/value pairs
MAX_CHART_SERIES = 12           # series in a chart
MAX_CHART_POINTS = 500          # points per series
MAX_CELL_CHARS = 2000           # any single string cell / value / label
MAX_TITLE_CHARS = 300           # the artifact title
MAX_FORECAST_DAYS = 16          # weather forecast entries (Open-Meteo's max)
MAX_DEVICES = 60                # controllable devices in a device_control widget
MAX_PRESENCE_ROOMS = 60         # rooms in a presence_map widget
MAX_PRESENCE_USERS = 50         # users in one room of a presence_map widget

# The domains a device_control widget may render a control for. This is a
# RENDER allowlist only — the actuation handler re-checks domain+action+entity
# server-side under the HA_CONTROL permission gate (the gate is in chat_handler's
# device_action frame route; the allowlist is in
# ha_glue/services/internal_tools.py::_device_action).
CONTROLLABLE_DOMAINS: frozenset[str] = frozenset({"light", "switch", "scene", "climate"})

# A turn may legitimately carry a couple of artifacts (table + chart); cap the
# count so a single turn can't fan out hundreds of frames.
MAX_ARTIFACTS_PER_TURN = 8


class ArtifactRejected(ValueError):
    """Raised when an artifact violates the kind allowlist or a DoS cap.

    The caller logs + drops the artifact (never emits it). It is a hard reject,
    not a fallback — the fallback path is a frontend concern (a shape the
    frontend can't render is shown as escaped text); the backend simply never
    lets an oversized/unknown-kind payload onto the wire.
    """


def _require_str(value: object, *, field: str, max_chars: int) -> str:
    """Coerce a scalar (str/int/float/bool) to a bounded string, else reject.

    Artifact cells are 'plain strings/numbers' (§3.3). We stringify numbers/bools
    so a model that emits ``42`` (not ``"42"``) still renders; anything
    structural (dict/list/None) or over the per-cell cap is a hard reject.
    """
    if isinstance(value, bool):
        # bool is an int subclass — handle before the int/float branch so it
        # stringifies as "True"/"False" rather than "1"/"0".
        value = str(value)
    elif isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        raise ArtifactRejected(f"{field}: expected scalar, got {type(value).__name__}")
    if len(value) > max_chars:
        raise ArtifactRejected(f"{field}: {len(value)} chars exceeds cap {max_chars}")
    return value


def _validate_table(data: object) -> dict:
    if not isinstance(data, dict):
        raise ArtifactRejected("table.data: not an object")
    columns = data.get("columns")
    rows = data.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ArtifactRejected("table.data: columns and rows must be lists")
    if len(columns) > MAX_TABLE_COLUMNS:
        raise ArtifactRejected(f"table: {len(columns)} columns exceeds cap {MAX_TABLE_COLUMNS}")
    if len(rows) > MAX_TABLE_ROWS:
        raise ArtifactRejected(f"table: {len(rows)} rows exceeds cap {MAX_TABLE_ROWS}")
    out_cols = [_require_str(c, field="table.column", max_chars=MAX_CELL_CHARS) for c in columns]
    out_rows: list[list[str]] = []
    for r in rows:
        if not isinstance(r, list):
            raise ArtifactRejected("table.row: not a list")
        if len(r) > MAX_TABLE_COLUMNS:
            raise ArtifactRejected(f"table.row: {len(r)} cells exceeds cap {MAX_TABLE_COLUMNS}")
        out_rows.append([_require_str(c, field="table.cell", max_chars=MAX_CELL_CHARS) for c in r])
    return {"columns": out_cols, "rows": out_rows}


def _validate_list(data: object) -> dict:
    if not isinstance(data, dict):
        raise ArtifactRejected("list.data: not an object")
    items = data.get("items")
    if not isinstance(items, list):
        raise ArtifactRejected("list.data: items must be a list")
    if len(items) > MAX_LIST_ITEMS:
        raise ArtifactRejected(f"list: {len(items)} items exceeds cap {MAX_LIST_ITEMS}")
    out = {"items": [_require_str(i, field="list.item", max_chars=MAX_CELL_CHARS) for i in items]}
    if "ordered" in data:
        out["ordered"] = bool(data["ordered"])
    return out


def _validate_keyvalue(data: object) -> dict:
    if not isinstance(data, dict):
        raise ArtifactRejected("keyvalue.data: not an object")
    pairs = data.get("pairs")
    if not isinstance(pairs, list):
        raise ArtifactRejected("keyvalue.data: pairs must be a list")
    if len(pairs) > MAX_KEYVALUE_PAIRS:
        raise ArtifactRejected(f"keyvalue: {len(pairs)} pairs exceeds cap {MAX_KEYVALUE_PAIRS}")
    out_pairs: list[dict] = []
    for p in pairs:
        if not isinstance(p, dict) or "key" not in p or "value" not in p:
            raise ArtifactRejected("keyvalue.pair: must be {key, value}")
        out_pairs.append({
            "key": _require_str(p["key"], field="keyvalue.key", max_chars=MAX_CELL_CHARS),
            "value": _require_str(p["value"], field="keyvalue.value", max_chars=MAX_CELL_CHARS),
        })
    return {"pairs": out_pairs}


def _finite_number(value: object, *, field: str) -> float:
    """Reject non-numeric / NaN / Infinity coordinates.

    A non-finite x/y would blow the SVG viewBox the frontend computes (a DoS) —
    so the cap gate refuses it server-side too (defense in depth; the frontend
    zod validator also coerces). int/float only; NaN and ±Inf are rejected.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArtifactRejected(f"{field}: expected a finite number")
    f = float(value)
    if f != f or f in (float("inf"), float("-inf")):  # NaN != NaN
        raise ArtifactRejected(f"{field}: non-finite number")
    return f


def _validate_chart(data: object) -> dict:
    if not isinstance(data, dict):
        raise ArtifactRejected("chart.data: not an object")
    chart_type = data.get("chartType")
    if chart_type not in ("bar", "line"):
        raise ArtifactRejected("chart.chartType: must be 'bar' or 'line'")
    series = data.get("series")
    if not isinstance(series, list):
        raise ArtifactRejected("chart.data: series must be a list")
    if len(series) > MAX_CHART_SERIES:
        raise ArtifactRejected(f"chart: {len(series)} series exceeds cap {MAX_CHART_SERIES}")
    out_series: list[dict] = []
    for s in series:
        if not isinstance(s, dict):
            raise ArtifactRejected("chart.series: not an object")
        points = s.get("points")
        if not isinstance(points, list):
            raise ArtifactRejected("chart.series.points: must be a list")
        if len(points) > MAX_CHART_POINTS:
            raise ArtifactRejected(f"chart: {len(points)} points exceeds cap {MAX_CHART_POINTS}")
        out_points: list[dict] = []
        for pt in points:
            if not isinstance(pt, dict):
                raise ArtifactRejected("chart.point: not an object")
            out_points.append({
                "x": _finite_number(pt.get("x"), field="chart.point.x"),
                "y": _finite_number(pt.get("y"), field="chart.point.y"),
            })
        out_series.append({
            "label": _require_str(s.get("label", ""), field="chart.series.label", max_chars=MAX_CELL_CHARS),
            "points": out_points,
        })
    return {"chartType": chart_type, "series": out_series}


def _opt_finite(value: object, *, field: str) -> float | None:
    """A finite number when present, else None (for optional weather fields)."""
    if value is None:
        return None
    return _finite_number(value, field=field)


def _validate_weather(data: object) -> dict:
    """A weather widget (Gen-UI): a location + current conditions + a short
    daily forecast. Numbers stay numeric (the renderer formats them); strings
    are bounded; the forecast is capped. The WMO ``code`` (int) drives the
    frontend's condition icon — an unknown code falls back to a neutral icon.
    """
    if not isinstance(data, dict):
        raise ArtifactRejected("weather.data: not an object")
    location = _require_str(
        data.get("location", ""), field="weather.location", max_chars=MAX_CELL_CHARS
    )
    cur = data.get("current")
    if not isinstance(cur, dict):
        raise ArtifactRejected("weather.current: must be an object")
    out_current: dict = {
        "temp": _finite_number(cur.get("temp"), field="weather.current.temp"),
        "unit": _require_str(cur.get("unit", "°C"), field="weather.current.unit", max_chars=8),
        "code": int(_finite_number(cur.get("code", 0), field="weather.current.code")),
        "condition": _require_str(
            cur.get("condition", ""), field="weather.current.condition", max_chars=MAX_CELL_CHARS
        ),
    }
    for opt in ("feelsLike", "humidity", "windSpeed", "high", "low"):
        v = _opt_finite(cur.get(opt), field=f"weather.current.{opt}")
        if v is not None:
            out_current[opt] = v

    out: dict = {"location": location, "current": out_current}

    forecast = data.get("forecast")
    if isinstance(forecast, list):
        if len(forecast) > MAX_FORECAST_DAYS:
            raise ArtifactRejected(
                f"weather: {len(forecast)} forecast days exceeds cap {MAX_FORECAST_DAYS}"
            )
        out_forecast: list[dict] = []
        for d in forecast:
            if not isinstance(d, dict):
                raise ArtifactRejected("weather.forecast[]: not an object")
            entry: dict = {
                "date": _require_str(d.get("date", ""), field="weather.forecast.date", max_chars=64),
                "code": int(_finite_number(d.get("code", 0), field="weather.forecast.code")),
                "high": _finite_number(d.get("high"), field="weather.forecast.high"),
                "low": _finite_number(d.get("low"), field="weather.forecast.low"),
            }
            cond = d.get("condition")
            if cond is not None:
                entry["condition"] = _require_str(
                    cond, field="weather.forecast.condition", max_chars=MAX_CELL_CHARS
                )
            pc = _opt_finite(d.get("precipChance"), field="weather.forecast.precipChance")
            if pc is not None:
                entry["precipChance"] = pc
            out_forecast.append(entry)
        out["forecast"] = out_forecast

    return out


def _validate_device_control(data: object) -> dict:
    """An INTERACTIVE device-control widget: a list of controllable HA devices
    (lights/switches → on/off toggle; scenes → activate). This validator is the
    DoS/shape gate only — clicking a control sends a separate `device_action` WS
    frame that is RE-VALIDATED server-side (HA_CONTROL permission + domain/action/
    entity allowlist in `device_action_service`); the widget cannot itself grant
    any control the user doesn't already have via the agent. A device whose
    domain isn't in `CONTROLLABLE_DOMAINS` is dropped (not rejected) so a mixed
    entity map still renders the controllable subset.
    """
    if not isinstance(data, dict):
        raise ArtifactRejected("device_control.data: not an object")
    devices = data.get("devices")
    if not isinstance(devices, list):
        raise ArtifactRejected("device_control.data: devices must be a list")
    if len(devices) > MAX_DEVICES:
        raise ArtifactRejected(f"device_control: {len(devices)} devices exceeds cap {MAX_DEVICES}")
    out_devices: list[dict] = []
    for d in devices:
        if not isinstance(d, dict):
            raise ArtifactRejected("device_control.device: not an object")
        entity_id = _require_str(d.get("entity_id", ""), field="device.entity_id", max_chars=255)
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        if domain not in CONTROLLABLE_DOMAINS:
            continue  # render only the controllable subset
        dev: dict = {
            "entity_id": entity_id,
            "domain": domain,
            "name": _require_str(d.get("name", entity_id), field="device.name", max_chars=MAX_CELL_CHARS),
            "state": _require_str(d.get("state", "unknown"), field="device.state", max_chars=64),
        }
        if d.get("room"):
            dev["room"] = _require_str(d["room"], field="device.room", max_chars=MAX_CELL_CHARS)
        # Optional continuous-control fields: brightness (lights, 0-100) and the
        # climate setpoint set (currentTemp/targetTemp/minTemp/maxTemp/tempStep).
        # Numeric, finite when present; the frontend renders a slider / stepper.
        for fld in ("brightness", "currentTemp", "targetTemp", "minTemp", "maxTemp", "tempStep"):
            v = _opt_finite(d.get(fld), field=f"device.{fld}")
            if v is not None:
                dev[fld] = v
        out_devices.append(dev)
    return {"devices": out_devices}


def _validate_presence_map(data: object) -> dict:
    """A read-only presence map: rooms, each with the people currently present.
    Pure presentation of live presence data (no actions). Capped per the DoS
    gate; strings bounded.
    """
    if not isinstance(data, dict):
        raise ArtifactRejected("presence_map.data: not an object")
    rooms = data.get("rooms")
    if not isinstance(rooms, list):
        raise ArtifactRejected("presence_map.data: rooms must be a list")
    if len(rooms) > MAX_PRESENCE_ROOMS:
        raise ArtifactRejected(f"presence_map: {len(rooms)} rooms exceeds cap {MAX_PRESENCE_ROOMS}")
    out_rooms: list[dict] = []
    for r in rooms:
        if not isinstance(r, dict):
            raise ArtifactRejected("presence_map.room: not an object")
        users = r.get("users", [])
        if not isinstance(users, list):
            raise ArtifactRejected("presence_map.room.users: must be a list")
        if len(users) > MAX_PRESENCE_USERS:
            raise ArtifactRejected(f"presence_map: {len(users)} users exceeds cap {MAX_PRESENCE_USERS}")
        out_rooms.append({
            "room": _require_str(r.get("room", ""), field="presence_map.room", max_chars=MAX_CELL_CHARS),
            "users": [_require_str(u, field="presence_map.user", max_chars=MAX_CELL_CHARS) for u in users],
        })
    return {"rooms": out_rooms}


_VALIDATORS = {
    "table": _validate_table,
    "list": _validate_list,
    "keyvalue": _validate_keyvalue,
    "chart": _validate_chart,
    "weather": _validate_weather,
    "device_control": _validate_device_control,
    "presence_map": _validate_presence_map,
}


def validate_artifact(artifact: object) -> dict:
    """Validate one artifact's kind + caps; return the cleaned dict or raise.

    Returns a NEW sanitized dict (scalars coerced to bounded strings, numbers
    finite) — never mutates the input. Raises ``ArtifactRejected`` on an unknown
    kind, a missing id, or any cap violation. The frontend zod validator owns
    the precise shape; this is the DoS wall only.
    """
    if not isinstance(artifact, dict):
        raise ArtifactRejected("artifact: not an object")
    kind = artifact.get("kind")
    if kind not in ALLOWED_KINDS:
        raise ArtifactRejected(f"artifact.kind: {kind!r} not in allowlist {sorted(ALLOWED_KINDS)}")
    art_id = artifact.get("id")
    if not isinstance(art_id, str) or not art_id:
        raise ArtifactRejected("artifact.id: required non-empty string")

    cleaned: dict = {
        "id": art_id,
        "kind": kind,
        "data": _VALIDATORS[kind](artifact.get("data")),
        # Default partial=False; streaming producers set it true on in-flight frames.
        "partial": bool(artifact.get("partial", False)),
    }
    title = artifact.get("title")
    if title is not None:
        cleaned["title"] = _require_str(title, field="artifact.title", max_chars=MAX_TITLE_CHARS)
    return cleaned


def validate_artifacts(artifacts: object) -> list[dict]:
    """Validate a turn's artifact list. Drops rejects (logged), caps the count.

    Used by the chat handler when a hook / sub-intent / orchestration result
    returns an ``artifacts`` list alongside (or instead of) a ``card``. A single
    bad artifact is dropped without killing the rest — the producer is the
    untrusted boundary, so be lenient at the list level but strict per item.
    """
    if not isinstance(artifacts, list):
        return []
    out: list[dict] = []
    for a in artifacts:
        if len(out) >= MAX_ARTIFACTS_PER_TURN:
            logger.warning(
                "artifact: dropping extra artifacts beyond cap %d", MAX_ARTIFACTS_PER_TURN
            )
            break
        try:
            out.append(validate_artifact(a))
        except ArtifactRejected as e:
            logger.warning("artifact rejected (dropped): %s", e)
    return out


def build_artifact_frame(artifact: dict, replace_text: str | None = None) -> dict:
    """Build the `artifact` WS frame for a single (already-validated) artifact."""
    frame: dict = {"type": "artifact", "artifact": artifact}
    if replace_text:
        frame["replace_text"] = replace_text
    return frame


def merge_artifacts_into_metadata(metadata: dict, artifacts: list[dict]) -> None:
    """Persist validated artifacts into ``message_metadata["artifacts"]`` (array).

    Keyed by ``id`` so a streaming re-emit / second frame for the same artifact
    replaces (does not duplicate) the stored entry — mirrors the frontend's
    keyed-append idempotency on the persistence side. Mutates ``metadata`` in
    place. No-op for an empty list (key omitted, like ``sources``).
    """
    if not artifacts:
        return
    existing = metadata.get("artifacts")
    by_id: dict[str, dict] = {}
    if isinstance(existing, list):
        for a in existing:
            if isinstance(a, dict) and isinstance(a.get("id"), str):
                by_id[a["id"]] = a
    for a in artifacts:
        by_id[a["id"]] = a
    metadata["artifacts"] = list(by_id.values())
