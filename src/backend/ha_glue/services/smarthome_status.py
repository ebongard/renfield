"""
Smart-home status → typed `table` artifact (the first Lane A artifact producer).

This is the FIRST thing in Renfield that emits a chat artifact (PR #801 added the
plumbing — `services/artifact_service.py` + the three `_emit_turn_artifacts` seams
in `chat_handler.py` — but nothing produced one). A natural German status/overview
request ("Wie ist der Status im Haus?", "Zeig mir den Smart-Home-Status",
"Übersicht der Geräte") now returns a prose answer AND a typed `table` artifact
that renders inline + persists + rehydrates on history reload.

Seam: the **`dispatch_sub_intent` hook** (`smart_home/status`, handler
``smarthome_status``). The chat handler's sub-intent path accumulates the returned
``artifacts`` into ``turn_artifacts`` BEFORE the assistant message is persisted, so
this rehydrates on reload (unlike the live-only ``build_assistant_card`` hook). The
orchestration card path was rejected: it only fires for multi-domain queries when
``agent_orchestrator_enabled`` is set, and a status request is single-domain.

Data source: ``HomeAssistantClient.get_entity_map()`` — the SAME cached (60s TTL)
entity view the intent recognizer uses. No new HA client, no new MCP call. Each
entry already carries ``entity_id``, ``friendly_name``, ``domain``, ``room`` and
``state``.

Shape: a `table` (not `keyvalue`) because the data is three-dimensional
(room × device × state); a flat key/value list would either lose the room grouping
or collide keys for same-named devices in different rooms.

The producer is INERT when ``settings.artifacts_typed_enabled`` is off — it returns
``handled=False`` (declines the turn so the normal agent answers) WITHOUT doing the
HA fetch, so the feature ships dark with zero extra HA load.
"""
from __future__ import annotations

import uuid
from typing import Any

from loguru import logger

from utils.config import settings

# How many device rows we put in the artifact before truncating. Well under the
# backend table cap (MAX_TABLE_ROWS = 200); a household with more entities than
# this gets a truncation note in the prose (never a silent cut).
MAX_STATUS_ROWS = 120

# Domains worth surfacing in a "house status" overview, in display priority order
# (controllable / safety-relevant first). get_entity_map already filters to a
# relevant-domain superset; this orders + labels them.
_DOMAIN_LABELS_DE: dict[str, str] = {
    "light": "Licht",
    "switch": "Schalter",
    "climate": "Heizung",
    "cover": "Rollladen",
    "lock": "Schloss",
    "fan": "Lüfter",
    "media_player": "Medien",
    "binary_sensor": "Sensor",
    "sensor": "Sensor",
    "vacuum": "Staubsauger",
    "camera": "Kamera",
    "alarm_control_panel": "Alarm",
    "scene": "Szene",
}
_DOMAIN_LABELS_EN: dict[str, str] = {
    "light": "Light",
    "switch": "Switch",
    "climate": "Climate",
    "cover": "Cover",
    "lock": "Lock",
    "fan": "Fan",
    "media_player": "Media",
    "binary_sensor": "Sensor",
    "sensor": "Sensor",
    "vacuum": "Vacuum",
    "camera": "Camera",
    "alarm_control_panel": "Alarm",
    "scene": "Scene",
}
# Sort priority for stable, sensible grouping within a room.
_DOMAIN_ORDER = list(_DOMAIN_LABELS_DE.keys())


def _device_label(friendly_name: str, entity_id: str) -> str:
    """Human device name: friendly_name, else the entity-id local part."""
    if friendly_name:
        return friendly_name
    if "." in entity_id:
        return entity_id.split(".", 1)[1].replace("_", " ").strip() or entity_id
    return entity_id or "?"


def build_status_table(entity_map: list[dict], lang: str = "de") -> dict | None:
    """Build ONE typed `table` artifact (grouped by room) from an HA entity map.

    Returns the artifact dict (kind=table) or ``None`` when there is nothing to
    show (HA unavailable / empty map) — the caller then answers prose-only, never
    a crash and never an empty table. Rows beyond ``MAX_STATUS_ROWS`` are dropped
    here and reported via the returned ``_truncated`` flag (see the wrapper) so the
    prose can mention it.
    """
    if not entity_map:
        return None

    is_de = lang.startswith("de")
    domain_labels = _DOMAIN_LABELS_DE if is_de else _DOMAIN_LABELS_EN
    no_room = "Ohne Raum" if is_de else "No room"

    # Sort by (room, domain-priority, device name) for a stable, grouped table.
    def _sort_key(e: dict) -> tuple:
        room = (e.get("room") or "").lower()
        domain = e.get("domain") or ""
        try:
            dpri = _DOMAIN_ORDER.index(domain)
        except ValueError:
            dpri = len(_DOMAIN_ORDER)
        name = _device_label(e.get("friendly_name", ""), e.get("entity_id", "")).lower()
        # Entities without a room sort last (empty room string → put after named).
        return (room == "", room, dpri, name)

    rows: list[list[str]] = []
    for e in sorted(entity_map, key=_sort_key):
        room_raw = e.get("room")
        room = room_raw.capitalize() if room_raw else no_room
        domain = e.get("domain") or ""
        device = _device_label(e.get("friendly_name", ""), e.get("entity_id", ""))
        state = str(e.get("state", "")) or ("unbekannt" if is_de else "unknown")
        dlabel = domain_labels.get(domain, domain or "?")
        # "<domain-label>: <device>" keeps the device cell self-describing without
        # adding a 4th column. State is the raw HA state (escaped by the renderer).
        rows.append([room, f"{dlabel}: {device}", state])

    truncated = len(rows) > MAX_STATUS_ROWS
    if truncated:
        rows = rows[:MAX_STATUS_ROWS]

    columns = ["Raum", "Gerät", "Status"] if is_de else ["Room", "Device", "State"]
    title = "Smart-Home-Status" if is_de else "Smart home status"

    return {
        "id": f"art_smarthome_status_{uuid.uuid4().hex[:12]}",
        "kind": "table",
        "title": title,
        "data": {"columns": columns, "rows": rows},
        # Internal hints (NOT part of the artifact contract — stripped before emit
        # by the caller): row-truncation flag + total entity count, so the prose
        # can mention truncation honestly.
        "_truncated": truncated,
        "_total": len(entity_map),
    }


def _prose(rows: int, total: int, truncated: bool, lang: str) -> str:
    """Short prose lede shown above the table."""
    is_de = lang.startswith("de")
    if rows == 0:
        return (
            "Ich konnte gerade keine Geräte aus dem Smart Home abrufen."
            if is_de else
            "I couldn't fetch any smart home devices right now."
        )
    if is_de:
        base = f"Hier ist der aktuelle Status von {rows} Geräten im Haus."
        if truncated:
            base += (
                f" (Es gibt insgesamt {total} Geräte — die Tabelle zeigt die "
                f"ersten {rows}.)"
            )
        return base
    base = f"Here is the current status of {rows} devices in the house."
    if truncated:
        base += f" (There are {total} devices in total — the table shows the first {rows}.)"
    return base


async def ha_dispatch_smarthome_status(
    *,
    role: str = "",
    sub_intent: str = "",
    handler_name: str = "",
    message: str = "",
    lang: str = "de",
    **_: Any,
) -> dict | None:
    """`dispatch_sub_intent` handler — owns the smart-home status/overview turn.

    Declines (returns ``None``) unless the router classified this as
    ``smart_home/status`` with our handler name AND the typed-artifacts flag is on.
    Declining lets the normal smart_home agent answer (so an actuation command like
    "mach das Licht an" — which never classifies as the ``status`` sub-intent —
    still actuates). On HA-unavailable it returns prose only (no artifact),
    never a crash.
    """
    # Only our sub-intent. Be lenient on handler_name (config may omit it) but
    # strict on the role + sub_intent pair.
    if role != "smart_home" or sub_intent != "status":
        return None
    if handler_name and handler_name != "smarthome_status":
        return None

    # Dark when the flag is off — decline WITHOUT touching HA so there is zero
    # extra load and the agent answers normally.
    if not settings.artifacts_typed_enabled:
        return None

    try:
        from ha_glue.integrations.homeassistant import HomeAssistantClient
        entity_map = await HomeAssistantClient().get_entity_map()
    except Exception as e:  # noqa: BLE001 — HA down must degrade to prose, not crash
        logger.warning(f"smarthome_status: HA entity map fetch failed: {e}")
        entity_map = []

    artifact = build_status_table(entity_map, lang=lang)
    if artifact is None:
        # HA unavailable / no devices → prose-only, no artifact.
        return {"handled": True, "answer": _prose(0, 0, False, lang), "card": None}

    truncated = bool(artifact.pop("_truncated", False))
    total = int(artifact.pop("_total", len(artifact["data"]["rows"])))
    rows = len(artifact["data"]["rows"])

    return {
        "handled": True,
        "answer": _prose(rows, total, truncated, lang),
        "card": None,
        "artifacts": [artifact],
    }
