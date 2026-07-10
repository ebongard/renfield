"""
Smart-home status → typed `table` artifact builder (a Lane A artifact producer).

``build_status_table`` turns the Home Assistant entity map into ONE typed `table`
artifact (room × device × state). It is called by the agent-callable
``internal.smart_home_overview`` tool (view=``status``) in
``ha_glue/services/internal_tools.py`` — the AGENT decides when to show the
overview, so this module is now a pure builder with no router/dispatch coupling.
(It used to own a ``dispatch_sub_intent`` hook that short-circuited the agent loop;
that misfired on router mis-classification and was removed — the LLM decides now.)

Data source: ``HomeAssistantClient.get_entity_map()`` — the SAME cached (60s TTL)
entity view the intent recognizer uses (the tool fetches it via ``_safe_entity_map``
in ``smarthome_artifacts``). Each entry already carries ``entity_id``,
``friendly_name``, ``domain``, ``room`` and ``state``.

Shape: a `table` (not `keyvalue`) because the data is three-dimensional
(room × device × state); a flat key/value list would either lose the room grouping
or collide keys for same-named devices in different rooms.

``build_status_table`` returns ``None`` when there is nothing to show (HA
unavailable / empty map) so the tool answers prose-only — never a crash, never an
empty table.
"""
from __future__ import annotations

import uuid

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
