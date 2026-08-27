"""
Reminder Tool — Platform-owned agent tool.

`internal.create_reminder` lets the chat agent create a time-based reminder
("erinnere mich in 2 Minuten daran, X") that the reminder checker fires as a
proactive notification. Before this tool the ONLY create path was
`POST /api/reminders`, so the chat agent had no way to persist a reminder and
instead hallucinated a confirmation without storing anything (#1146).

Mirrors `services/memory_list_tool.py`: a flattened tool definition registered by
`agent_tools._register_internal_tools` + an async handler dispatched as a special
case in `action_executor` (which injects the authenticated `user_id` +
`session_id` — never from LLM params).

The runtime gate `settings.proactive_reminders_enabled` is re-asserted here (H4)
so the tool never persists a reminder the checker will never fire.
"""
from __future__ import annotations

from loguru import logger

from utils.config import settings

# Registered with the agent tool registry by
# `services/agent_tools.py::_register_internal_tools()`.
REMINDER_TOOL: dict = {
    "internal.create_reminder": {
        "description": (
            "Create a time-based reminder for the CURRENT user. Use this whenever "
            "the user asks to be reminded of something later ('erinnere mich in 30 "
            "Minuten daran, ...', 'remind me at 18:00 to ...', 'erinnere mich in 2 "
            "Stunden'). The reminder fires as a proactive notification at the given "
            "time — do NOT just confirm without calling this tool. Provide the "
            "reminder text AND when it should fire."
        ),
        "parameters": {
            "message": "What to remind the user about (required)",
            "trigger_at": (
                "When to fire, as a relative or absolute phrase: 'in 30 Minuten', "
                "'in 2 hours', 'in 45 Sekunden', 'um 18:00' / 'at 18:00', or an ISO "
                "datetime (required)"
            ),
            "room": "Optional room name to deliver the spoken reminder to (optional)",
        },
    },
}


async def create_reminder(
    params: dict,
    user_id: int | None = None,
    session_id: str | None = None,
) -> dict:
    """Create a reminder for the asking user via the shared ReminderService.

    `user_id`/`session_id` are injected by `action_executor` from the
    authenticated context — never taken from LLM-supplied params (the model
    can't know its own id, and a reminder must belong to its creator).
    """
    # H4: re-assert the runtime gate in the handler. A reminder created while the
    # checker is off would never fire — refuse rather than persist a dead row.
    if not settings.proactive_reminders_enabled:
        return {
            "success": False,
            "message": "Reminders are currently disabled on this instance.",
            "action_taken": False,
        }

    message = (params.get("message") or "").strip()
    # Accept the canonical key plus the two aliases the LLM tends to emit.
    trigger_at = (
        params.get("trigger_at")
        or params.get("when")
        or params.get("trigger_at_str")
        or ""
    ).strip()
    room = (params.get("room") or "").strip() or None

    if not message:
        return {
            "success": False,
            "message": "I need to know WHAT to remind you about.",
            "action_taken": False,
        }
    if not trigger_at:
        return {
            "success": False,
            "message": "I need to know WHEN to remind you (e.g. 'in 30 Minuten', 'um 18:00').",
            "action_taken": False,
        }

    try:
        from services.database import AsyncSessionLocal
        from services.reminder_service import ReminderService

        async with AsyncSessionLocal() as db:
            reminder = await ReminderService(db).create_reminder(
                message=message,
                trigger_at_str=trigger_at,
                room=room,
                user_id=user_id,
                session_id=session_id,
            )

        return {
            "success": True,
            "message": f"Reminder set for {reminder.trigger_at:%Y-%m-%d %H:%M}: {message}",
            "action_taken": True,
            "data": {
                "reminder_id": reminder.id,
                "trigger_at": reminder.trigger_at.isoformat(),
                "room": reminder.room_name,
            },
        }
    except ValueError as e:
        # Unparseable time or a time in the past — a user-actionable message, not a crash.
        return {"success": False, "message": str(e), "action_taken": False}
    except Exception as e:  # noqa: BLE001 — surface any unexpected failure as a tool error
        logger.error(f"Error in create_reminder tool: {e}")
        return {"success": False, "message": f"Reminder error: {e!s}", "action_taken": False}
