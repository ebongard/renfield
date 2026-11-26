"""`internal.find_contact` -- read a person's / organization's numbers (#1240).

Two modes:
  - ``name``: the contact points of the entities with that name or alias;
  - ``number``: reverse lookup, "whose number is this?".

Both are circle-filtered for the authenticated asker (``user_id`` injected by
``action_executor``, never from LLM params). With auth on, an identified user
without ``KG_VIEW`` is refused (the REST read needs it too); an unidentified turn
(``user_permissions=None``) gets public values only, via the circle filter. Contact points never enter the
general RAG context (design §9): only a question that asks for them reaches them.
"""
from __future__ import annotations

from loguru import logger

from models.database import CONTACT_POINT_KINDS
from models.permissions import Permission, has_permission
from services.database import AsyncSessionLocal
from utils.config import settings
from utils.log_safety import error_text_without_content

FIND_CONTACT_TOOL: dict = {
    "internal.find_contact": {
        "description": (
            "Look up stored PHONE / MOBILE / FAX numbers of a person or organization "
            "from the knowledge graph ('Wie ist die Handynummer von Hans?', 'welche "
            "Telefonnummer hat die Praxis Beispiel?'), or find out WHOSE number a given "
            "number is ('wem gehört die Nummer 0170 …?'). Pass either 'name' or 'number'."
        ),
        "parameters": {
            "name": "Name of the person or organization (optional if 'number' is given)",
            "number": "A phone number to look up in reverse (optional if 'name' is given)",
            "kind": "Optional filter: phone, mobile or fax",
        },
    },
}


def _point(hit) -> dict:
    cp = hit.point
    return {
        "name": hit.entity_name,
        "entity_type": hit.entity_type,
        "kind": cp.kind,
        "label": cp.label,
        "value": cp.value,
        "normalized": cp.normalized,
        "source": cp.source_type,
    }


async def find_contact(
    parameters: dict, user_id: int | None = None, user_permissions: list[str] | None = None,
) -> dict:
    if not settings.contact_points_enabled:
        return {
            "success": True, "action_taken": False,
            "message": "Kontaktdaten im Wissensgraphen sind auf dieser Instanz nicht aktiviert.",
            "data": {"enabled": False, "contacts": []},
        }
    if settings.auth_enabled and user_permissions is not None:
        if not has_permission(user_permissions, Permission.KG_VIEW):
            return {
                "success": False, "action_taken": False,
                "message": "Zum Lesen von Kontaktdaten fehlt die Berechtigung (Wissensgraph).",
                "data": {"contacts": []},
            }
    name = str(parameters.get("name") or "").strip()
    number = str(parameters.get("number") or "").strip()
    kind = str(parameters.get("kind") or "").strip().lower() or None
    if kind is not None and kind not in CONTACT_POINT_KINDS:
        kind = None
    if not name and not number:
        return {"success": False, "message": "Bitte einen Namen oder eine Nummer angeben.",
                "data": {"contacts": []}}
    try:
        from services.contact_points import ContactPointService

        async with AsyncSessionLocal() as db:
            svc = ContactPointService(db)
            if number:
                hits = await svc.lookup_number(number, user_id)
            else:
                hits = await svc.find_for_name(name, user_id, kind=kind)
            contacts = [_point(h) for h in hits]
    except Exception as e:
        logger.warning(f"internal.find_contact failed: {error_text_without_content(e)}")
        return {"success": False, "message": "Kontaktdaten konnten nicht gelesen werden.",
                "data": {"contacts": []}}
    if not contacts:
        return {"success": True, "action_taken": False,
                "message": "Keine gespeicherten Kontaktdaten gefunden.",
                "data": {"contacts": []}}
    return {"success": True, "action_taken": False,
            "message": f"{len(contacts)} Kontaktdaten gefunden.",
            "data": {"contacts": contacts}}
