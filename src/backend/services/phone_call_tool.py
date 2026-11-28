"""Phone call tool (#1683, Telefonie Phase 4) — platform-owned agent tool.

``internal.phone_call`` rings the ASKING user's own confirmed number
(``users.call_number``) and speaks the message ("Ruf mich an und sag mir ...").
Registered by ``agent_tools._register_internal_tools`` only while
TELEPHONY_OUTBOUND_ENABLED; dispatched by ``action_executor`` with the injected
``user_id`` / ``user_permissions`` -- the number is never a tool argument.

Fail-closed (it rings a real phone): auth off, an unidentified turn
(``user_permissions is None``: device/satellite token, unrecognised voice), a user
without ``telephony.call`` or without a confirmed number are all refused. The
handler re-asserts the flag (H4).
"""
from __future__ import annotations

from loguru import logger

from utils.config import settings

PHONE_CALL_TOOL: dict = {
    "internal.phone_call": {
        "description": (
            "Call the CURRENT user on their own phone and speak a short message. Use "
            "this when the user explicitly asks to be called ('ruf mich an und sag mir "
            "...', 'ruf mich an', 'call me and tell me ...'). Only the user's own, "
            "confirmed number can be called -- never another person or number. Put "
            "the words to be spoken into 'message' (German, short, no markup)."
        ),
        "parameters": {
            "message": "The message to speak on the call (short plain text).",
        },
    }
}


def _refuse(message: str) -> dict:
    return {"success": False, "message": message, "action_taken": False}


async def phone_call(params: dict, user_id: int | None = None, user_permissions: list | None = None) -> dict:
    from models.permissions import Permission, has_permission
    from services import telephony_outbound

    if not telephony_outbound.enabled():
        return _refuse("Anrufe sind auf dieser Instanz nicht eingeschaltet.")
    if not settings.auth_enabled or user_id is None or user_permissions is None:
        return _refuse("Ich kann nur angemeldete, erkannte Nutzer anrufen.")
    if not has_permission(user_permissions, Permission.TELEPHONY_CALL):
        return _refuse("Du hast keine Berechtigung, von mir angerufen zu werden.")
    message = " ".join(str(params.get("message") or "").split())[: settings.telephony_call_max_chars]
    if not message:
        return _refuse("Was soll ich dir am Telefon sagen?")

    from models.database import User
    from services.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        number = user.call_number if user is not None and user.call_number_verified_at else None
    if not number:
        return _refuse(
            "Für dich ist keine bestätigte Telefonnummer hinterlegt. "
            "Du kannst sie in den Kontoeinstellungen eintragen."
        )
    try:
        await telephony_outbound.place_call(user_id=user_id, number=number, text=message)
    except telephony_outbound.CallError as e:
        logger.info(f"phone_call refused: {e.code}")
        return _refuse(e.reason)
    return {"success": True, "message": "Ich rufe dich jetzt an.", "action_taken": True}
