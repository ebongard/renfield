"""
Three more Lane A artifact producers — lighting up the `keyvalue`, `list` and
`chart` renderers with REAL Home Assistant data.

These mirror the FIRST producer (``smarthome_status.py`` → `table`) exactly:

  * each is a ``dispatch_sub_intent`` hook for a distinct ``smart_home/<x>``
    sub-intent and returns ``{"handled": True, "answer": <prose>, "card": None,
    "artifacts": [<typed dict>]}``;
  * the single data source is ``HomeAssistantClient.get_entity_map()`` — the SAME
    cached (60s TTL) entity view ``smarthome_status`` uses. No new HA client, no
    new MCP call, no DB session (so none of the asyncpg-under-pytest segfault
    risk);
  * each is INERT when ``settings.artifacts_typed_enabled`` is off — it returns
    ``None`` (declines, the normal agent answers) BEFORE touching HA, so the
    feature ships dark with zero extra HA load;
  * each degrades gracefully (prose only, no artifact) when HA is unavailable or
    the relevant data is empty — never a crash, never an empty artifact;
  * each produces localized (de/en) prose, like ``smarthome_status``.

The three producers:

  1. ``smart_home/sensors``        → `keyvalue` (room → temperature · humidity)
  2. ``smart_home/active_devices`` → `list`     ("<Raum>: <Gerät>" currently on)
  3. ``smart_home/devices_per_room`` → `chart`  (bar: device count per room)

CHART SOURCE CHOICE: device-count-per-room (option (a) in the brief). It is real,
numeric, and derivable from the cached ``get_entity_map()`` alone — no history and
no presence-analytics DB plumbing (option (b) would need an ``AsyncSessionLocal``
session in the producer path, which the test-isolation lesson warns against and
which adds latency to a chat turn). A count-per-room bar exercises the hand-rolled
SVG bar chart + its CVD-safe palette with honest data.

Why these don't collide with actuation or with ``smart_home/status``: the router's
``_infer_sub_intent`` scores each sub-intent by how many of its comma-separated
keywords appear in the message and picks the highest. Each producer's keyword set
(in ``agent_roles.yaml``) is built from terms unique to it ("sensorwerte",
"temperaturen" / "eingeschaltet", "gerade an" / "geräte pro raum", "wie viele
geräte") and contains NO actuation verb ("mach … an", "schalte", "dimme"), so an
actuation command scores zero on all of them and falls through to the agent loop.
"""
from __future__ import annotations

import uuid
from typing import Any

from loguru import logger

from utils.config import settings

# --- caps (kept well under the artifact_service backend caps) ----------------
# keyvalue: MAX_KEYVALUE_PAIRS=100 — a household has far fewer rooms.
MAX_SENSOR_PAIRS = 60
# list: MAX_LIST_ITEMS=200 — cap active devices comfortably under it.
MAX_ACTIVE_ITEMS = 120
# chart: MAX_CHART_POINTS=500 — rooms are few; cap defensively.
MAX_ROOM_BARS = 60


# Controllable / "is it doing something" domains and the HA states that count as
# "on" for the active-devices list. media_player "playing"/"paused" count as on;
# "idle"/"off"/"standby" do not. Sensors/binary_sensors are excluded — they are
# not things you "switch on", and a binary_sensor "on" (e.g. a window open) is not
# what "was ist eingeschaltet" means.
_ACTIVE_DOMAINS = {"light", "switch", "fan", "media_player", "vacuum"}
_ON_STATES = {"on", "playing", "paused", "cleaning", "open", "true"}

# Device-domain labels (shared vocabulary with smarthome_status, kept local so
# this module has no import coupling to it).
_DOMAIN_LABELS_DE: dict[str, str] = {
    "light": "Licht", "switch": "Schalter", "climate": "Heizung",
    "cover": "Rollladen", "lock": "Schloss", "fan": "Lüfter",
    "media_player": "Medien", "binary_sensor": "Sensor", "sensor": "Sensor",
    "vacuum": "Staubsauger", "camera": "Kamera",
    "alarm_control_panel": "Alarm", "scene": "Szene",
}
_DOMAIN_LABELS_EN: dict[str, str] = {
    "light": "Light", "switch": "Switch", "climate": "Climate",
    "cover": "Cover", "lock": "Lock", "fan": "Fan",
    "media_player": "Media", "binary_sensor": "Sensor", "sensor": "Sensor",
    "vacuum": "Vacuum", "camera": "Camera",
    "alarm_control_panel": "Alarm", "scene": "Scene",
}


def _device_label(friendly_name: str, entity_id: str) -> str:
    """Human device name: friendly_name, else the entity-id local part."""
    if friendly_name:
        return friendly_name
    if "." in entity_id:
        return entity_id.split(".", 1)[1].replace("_", " ").strip() or entity_id
    return entity_id or "?"


def _room_label(room_raw: str | None, *, is_de: bool) -> str:
    if room_raw:
        return room_raw.capitalize()
    return "Ohne Raum" if is_de else "No room"


# ===========================================================================
# Producer 1: sensors → `keyvalue` (room → temperature · humidity)
# ===========================================================================

# We only have entity_map's flat view (entity_id, friendly_name, domain, room,
# state) — no attributes/units. So we infer a reading's KIND from the entity
# name (German + English variants) and a numeric state. The state IS the value.
_TEMP_TOKENS = ("temperatur", "temperature", "temp", "thermo")
_HUMID_TOKENS = ("feucht", "humidity", "luftfeuchte", "luftfeuchtigkeit")


def _looks_numeric(state: str) -> bool:
    """True if the HA state is a finite decimal (e.g. '21.4', '45')."""
    try:
        float(state.replace(",", "."))
        return True
    except (TypeError, ValueError, AttributeError):
        return False


def _classify_reading(entity_id: str, friendly_name: str) -> str | None:
    """'temp' | 'humidity' | None, inferred from the entity/friendly name."""
    hay = f"{entity_id} {friendly_name}".lower()
    if any(t in hay for t in _HUMID_TOKENS):
        return "humidity"
    if any(t in hay for t in _TEMP_TOKENS):
        return "temp"
    return None


def build_sensor_keyvalue(entity_map: list[dict], lang: str = "de") -> dict | None:
    """Build ONE `keyvalue` artifact of per-room temperature/humidity readings.

    Source rows: sensor entities whose name marks them temperature/humidity AND
    whose state is numeric. We fold per room into a single value string
    "21.4 °C · 45 %". Returns ``None`` when there is nothing to show (so the
    caller answers prose-only).
    """
    if not entity_map:
        return None
    is_de = lang.startswith("de")

    # room -> {"temp": "21.4", "humidity": "45"} (first reading of each kind wins
    # per room — a stable, deterministic pick by sort order).
    by_room: dict[str, dict[str, str]] = {}
    order: list[str] = []  # preserve first-seen room order for stable output

    for e in sorted(
        entity_map,
        key=lambda x: ((x.get("room") or "￿"), x.get("entity_id") or ""),
    ):
        domain = e.get("domain") or ""
        state = str(e.get("state", "") or "")
        entity_id = e.get("entity_id", "") or ""
        fname = e.get("friendly_name", "") or ""
        room = _room_label(e.get("room"), is_de=is_de)

        if domain != "sensor":
            # climate state is a mode ("heat"/"off"); without attributes we
            # cannot read current_temperature, so sensors are the honest source.
            continue
        kind = _classify_reading(entity_id, fname)
        if kind is None or not _looks_numeric(state):
            continue

        slot = by_room.get(room)
        if slot is None:
            slot = {}
            by_room[room] = slot
            order.append(room)
        if kind not in slot:
            slot[kind] = state

    if not by_room:
        return None

    def _fmt_value(slot: dict[str, str]) -> str:
        parts: list[str] = []
        if "temp" in slot:
            parts.append(f"{slot['temp']} °C")
        if "humidity" in slot:
            parts.append(f"{slot['humidity']} %")
        return " · ".join(parts)

    pairs = [
        {"key": room, "value": _fmt_value(by_room[room])}
        for room in order
        if _fmt_value(by_room[room])
    ]
    if not pairs:
        return None
    truncated = len(pairs) > MAX_SENSOR_PAIRS
    if truncated:
        pairs = pairs[:MAX_SENSOR_PAIRS]

    title = "Sensorwerte" if is_de else "Sensor readings"
    return {
        "id": f"art_sensors_{uuid.uuid4().hex[:12]}",
        "kind": "keyvalue",
        "title": title,
        "data": {"pairs": pairs},
        "_truncated": truncated,
        "_count": len(pairs),
    }


def _sensors_prose(count: int, truncated: bool, lang: str) -> str:
    is_de = lang.startswith("de")
    if count == 0:
        return (
            "Ich konnte gerade keine Sensorwerte (Temperatur/Luftfeuchte) abrufen."
            if is_de else
            "I couldn't fetch any sensor readings (temperature/humidity) right now."
        )
    if is_de:
        base = f"Hier sind die aktuellen Sensorwerte aus {count} Räumen."
        if truncated:
            base += f" (Gekürzt auf die ersten {count}.)"
        return base
    base = f"Here are the current sensor readings from {count} rooms."
    if truncated:
        base += f" (Truncated to the first {count}.)"
    return base


async def ha_dispatch_sensors(
    *,
    role: str = "",
    sub_intent: str = "",
    handler_name: str = "",
    lang: str = "de",
    **_: Any,
) -> dict | None:
    """`dispatch_sub_intent` handler for ``smart_home/sensors`` → `keyvalue`."""
    if role != "smart_home" or sub_intent != "sensors":
        return None
    if handler_name and handler_name != "smarthome_sensors":
        return None
    if not settings.artifacts_typed_enabled:
        return None

    entity_map = await _safe_entity_map("sensors")
    artifact = build_sensor_keyvalue(entity_map, lang=lang)
    if artifact is None:
        return {"handled": True, "answer": _sensors_prose(0, False, lang), "card": None}
    truncated = bool(artifact.pop("_truncated", False))
    count = int(artifact.pop("_count", len(artifact["data"]["pairs"])))
    return {
        "handled": True,
        "answer": _sensors_prose(count, truncated, lang),
        "card": None,
        "artifacts": [artifact],
    }


# ===========================================================================
# Producer 2: active_devices → `list` ("<Raum>: <Gerät>" currently on)
# ===========================================================================

def build_active_list(entity_map: list[dict], lang: str = "de") -> dict | None:
    """Build ONE `list` artifact of controllable devices that are currently on.

    Returns ``None`` when nothing is on (so the caller can say "nichts ist an"
    in prose rather than show an empty list).
    """
    if not entity_map:
        return None
    is_de = lang.startswith("de")
    labels = _DOMAIN_LABELS_DE if is_de else _DOMAIN_LABELS_EN

    items: list[str] = []
    for e in sorted(
        entity_map,
        key=lambda x: ((x.get("room") or "￿"), x.get("domain") or "",
                       _device_label(x.get("friendly_name", ""), x.get("entity_id", "")).lower()),
    ):
        domain = e.get("domain") or ""
        if domain not in _ACTIVE_DOMAINS:
            continue
        state = str(e.get("state", "") or "").lower()
        if state not in _ON_STATES:
            continue
        room = _room_label(e.get("room"), is_de=is_de)
        device = _device_label(e.get("friendly_name", ""), e.get("entity_id", ""))
        dlabel = labels.get(domain, domain or "?")
        items.append(f"{room}: {dlabel} – {device}")

    if not items:
        return None
    truncated = len(items) > MAX_ACTIVE_ITEMS
    if truncated:
        items = items[:MAX_ACTIVE_ITEMS]

    title = "Aktuell eingeschaltet" if is_de else "Currently on"
    return {
        "id": f"art_active_{uuid.uuid4().hex[:12]}",
        "kind": "list",
        "title": title,
        "data": {"ordered": False, "items": items},
        "_truncated": truncated,
        "_count": len(items),
    }


def _active_prose(count: int, truncated: bool, lang: str) -> str:
    is_de = lang.startswith("de")
    if count == 0:
        return (
            "Gerade ist nichts eingeschaltet (keine Lichter, Schalter, Lüfter, "
            "Medien oder Sauger aktiv)."
            if is_de else
            "Nothing is currently on (no lights, switches, fans, media or "
            "vacuums active)."
        )
    if is_de:
        base = f"Aktuell sind {count} Geräte eingeschaltet."
        if truncated:
            base += f" (Gekürzt auf die ersten {count}.)"
        return base
    base = f"There are {count} devices currently on."
    if truncated:
        base += f" (Truncated to the first {count}.)"
    return base


async def ha_dispatch_active_devices(
    *,
    role: str = "",
    sub_intent: str = "",
    handler_name: str = "",
    lang: str = "de",
    **_: Any,
) -> dict | None:
    """`dispatch_sub_intent` handler for ``smart_home/active_devices`` → `list`."""
    if role != "smart_home" or sub_intent != "active_devices":
        return None
    if handler_name and handler_name != "smarthome_active_devices":
        return None
    if not settings.artifacts_typed_enabled:
        return None

    entity_map = await _safe_entity_map("active_devices")
    artifact = build_active_list(entity_map, lang=lang)
    if artifact is None:
        # Either HA empty or nothing on — both answer "nothing is on" in prose.
        return {"handled": True, "answer": _active_prose(0, False, lang), "card": None}
    truncated = bool(artifact.pop("_truncated", False))
    count = int(artifact.pop("_count", len(artifact["data"]["items"])))
    return {
        "handled": True,
        "answer": _active_prose(count, truncated, lang),
        "card": None,
        "artifacts": [artifact],
    }


# ===========================================================================
# Producer 3: devices_per_room → `chart` (bar: device count per room)
# ===========================================================================

def build_devices_per_room_chart(entity_map: list[dict], lang: str = "de") -> dict | None:
    """Build ONE `chart` (bar) artifact: number of devices per room.

    One series, one point per room. The x coordinate is the bar index (a numeric
    x keeps the contract simple and finite). Returns ``None`` when there are no
    devices.
    """
    if not entity_map:
        return None
    is_de = lang.startswith("de")

    counts: dict[str, int] = {}
    for e in entity_map:
        room = _room_label(e.get("room"), is_de=is_de)
        counts[room] = counts.get(room, 0) + 1
    if not counts:
        return None

    # Sort rooms by descending count (most-populated first), then name for ties.
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    truncated = len(ordered) > MAX_ROOM_BARS
    if truncated:
        ordered = ordered[:MAX_ROOM_BARS]

    points = [{"x": float(i), "y": float(cnt)} for i, (_room, cnt) in enumerate(ordered)]
    series_label = "Geräte" if is_de else "Devices"
    title = "Geräte pro Raum" if is_de else "Devices per room"

    return {
        "id": f"art_devperroom_{uuid.uuid4().hex[:12]}",
        "kind": "chart",
        "title": title,
        "data": {
            "chartType": "bar",
            "series": [{"label": series_label, "points": points}],
        },
        # The renderer keys bars by series order; carry the room labels so the
        # prose can spell out which bar is which room (the chart kind has no
        # per-point label field in the contract).
        "_room_labels": [room for room, _ in ordered],
        "_truncated": truncated,
        "_count": len(ordered),
    }


def _devperroom_prose(room_labels: list[str], counts_for_rooms: list[int],
                      truncated: bool, lang: str) -> str:
    is_de = lang.startswith("de")
    if not room_labels:
        return (
            "Ich konnte gerade keine Geräte aus dem Smart Home abrufen."
            if is_de else
            "I couldn't fetch any smart home devices right now."
        )
    # A compact "Raum (n)" legend so the bar order is self-describing.
    legend = ", ".join(
        f"{r} ({c})" for r, c in zip(room_labels, counts_for_rooms)
    )
    if is_de:
        base = f"Geräte pro Raum (von links nach rechts): {legend}."
        if truncated:
            base += f" (Gekürzt auf die ersten {len(room_labels)} Räume.)"
        return base
    base = f"Devices per room (left to right): {legend}."
    if truncated:
        base += f" (Truncated to the first {len(room_labels)} rooms.)"
    return base


async def ha_dispatch_devices_per_room(
    *,
    role: str = "",
    sub_intent: str = "",
    handler_name: str = "",
    lang: str = "de",
    **_: Any,
) -> dict | None:
    """`dispatch_sub_intent` handler for ``smart_home/devices_per_room`` → `chart`."""
    if role != "smart_home" or sub_intent != "devices_per_room":
        return None
    if handler_name and handler_name != "smarthome_devices_per_room":
        return None
    if not settings.artifacts_typed_enabled:
        return None

    entity_map = await _safe_entity_map("devices_per_room")
    artifact = build_devices_per_room_chart(entity_map, lang=lang)
    if artifact is None:
        return {"handled": True, "answer": _devperroom_prose([], [], False, lang), "card": None}
    room_labels = artifact.pop("_room_labels", [])
    truncated = bool(artifact.pop("_truncated", False))
    artifact.pop("_count", None)
    counts = [int(p["y"]) for p in artifact["data"]["series"][0]["points"]]
    return {
        "handled": True,
        "answer": _devperroom_prose(room_labels, counts, truncated, lang),
        "card": None,
        "artifacts": [artifact],
    }


# ===========================================================================
# shared HA fetch (degrade to [] on any failure — prose-only path)
# ===========================================================================

async def _safe_entity_map(producer: str) -> list[dict]:
    """Fetch the cached HA entity map, returning [] (never raising) on failure."""
    try:
        from ha_glue.integrations.homeassistant import HomeAssistantClient
        return await HomeAssistantClient().get_entity_map()
    except Exception as e:  # noqa: BLE001 — HA down must degrade to prose, not crash
        logger.warning(f"smarthome_artifacts[{producer}]: HA entity map fetch failed: {e}")
        return []
