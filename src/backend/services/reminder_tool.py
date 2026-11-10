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

`internal.list_reminders` / `internal.cancel_reminder` (#264) answer "welche
Timer laufen?" and "brich den Nudel-Timer ab" by voice. Both are scoped to the
injected `user_id` — admins included, unlike `GET /api/reminders`, because a
spoken question about "my timers" means one's own.

The runtime gate `settings.proactive_reminders_enabled` is re-asserted in every
handler (H4) so the tools never act on reminders the checker will never fire.
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
    "internal.list_reminders": {
        "description": (
            "List the CURRENT user's pending reminders and timers with their local "
            "firing time ('welche Erinnerungen habe ich?', 'welche Timer laufen?', "
            "'wann werde ich erinnert?', 'what reminders are set?'). Read-only."
        ),
        "parameters": {},
    },
    "internal.cancel_reminder": {
        "description": (
            "Cancel ONE of the current user's pending reminders or timers "
            "('lösche die Erinnerung an den Ofen', 'brich den Nudel-Timer ab', "
            "'cancel the oven reminder'). Pass `reminder_id` from "
            "internal.list_reminders, or `match` with a word from the reminder "
            "text. If several reminders match, nothing is cancelled and the "
            "candidates are returned — ask the user which one."
        ),
        "parameters": {
            "reminder_id": "Id of the reminder to cancel, from internal.list_reminders (optional)",
            "match": "A word or phrase from the reminder text, used when no id is known (optional)",
        },
    },
}


def _disabled() -> dict:
    return {
        "success": False,
        "message": "Reminders are currently disabled on this instance.",
        "action_taken": False,
    }


def _describe(reminder) -> dict:
    """A pending reminder as the agent sees it: local time, never naive UTC."""
    from services.reminder_service import naive_utc_to_local

    local = naive_utc_to_local(reminder.trigger_at)
    return {
        "reminder_id": reminder.id,
        "message": reminder.message,
        "trigger_at": local.isoformat(timespec="minutes"),
        "room": reminder.room_name,
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
        return _disabled()

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
        from services.reminder_service import ReminderService, naive_utc_to_local

        async with AsyncSessionLocal() as db:
            reminder = await ReminderService(db).create_reminder(
                message=message,
                trigger_at_str=trigger_at,
                room=room,
                user_id=user_id,
                session_id=session_id,
            )

        # The row stores naive UTC; the user hears local time (an "um 18:00"
        # reminder was confirmed as "16:00" for a CEST user before #264).
        local = naive_utc_to_local(reminder.trigger_at)
        return {
            "success": True,
            "message": f"Reminder set for {local:%Y-%m-%d %H:%M} (local time): {message}",
            "action_taken": True,
            "data": {
                "reminder_id": reminder.id,
                "trigger_at": local.isoformat(timespec="minutes"),
                "room": reminder.room_name,
            },
        }
    except ValueError as e:
        # Unparseable time or a time in the past — a user-actionable message, not a crash.
        return {"success": False, "message": str(e), "action_taken": False}
    except Exception as e:
        logger.error(f"Error in create_reminder tool: {e}")
        return {"success": False, "message": f"Reminder error: {e!s}", "action_taken": False}


async def list_reminders(params: dict, user_id: int | None = None) -> dict:
    """List the asking user's pending reminders.

    Always scoped to the injected `user_id`, admins included: a reminder's text is
    personal, and "welche Timer laufen?" asks about one's own. An unidentified
    turn (`user_id=None`) sees only ownerless reminders, which fire publicly anyway.
    """
    if not settings.proactive_reminders_enabled:
        return _disabled()

    try:
        from services.database import AsyncSessionLocal
        from services.reminder_service import ReminderService

        async with AsyncSessionLocal() as db:
            reminders = await ReminderService(db).list_pending(
                user_id=user_id, restrict_to_user=True,
            )
        items = [_describe(r) for r in reminders]
        if not items:
            return {
                "success": True,
                "message": "No pending reminders.",
                "action_taken": False,
                "data": {"reminders": []},
            }
        lines = "\n".join(
            f"- #{i['reminder_id']} {i['trigger_at']}: {i['message']}" for i in items
        )
        return {
            "success": True,
            "message": f"{len(items)} pending reminder(s) (local time):\n{lines}",
            "action_taken": False,
            "data": {"reminders": items},
        }
    except Exception as e:
        logger.error(f"Error in list_reminders tool: {e}")
        return {"success": False, "message": f"Reminder error: {e!s}", "action_taken": False}


async def cancel_reminder(params: dict, user_id: int | None = None) -> dict:
    """Cancel one of the asking user's pending reminders, by id or by text match.

    Owner-scoped like `list_reminders`: someone else's reminder reads as missing.
    An ambiguous `match` cancels nothing and returns the candidates.
    """
    if not settings.proactive_reminders_enabled:
        return _disabled()

    raw_id = params.get("reminder_id")
    match = str(params.get("match") or "").strip()
    reminder_id: int | None = None
    if raw_id not in (None, ""):
        try:
            # LLMs send 5, "5", "#5" or 5.0 — all mean reminder 5; 5.5 means nothing.
            as_float = float(str(raw_id).lstrip("#"))
            if not as_float.is_integer():
                raise ValueError(raw_id)
            reminder_id = int(as_float)
        except ValueError:
            return {
                "success": False,
                "message": f"'{raw_id}' is not a reminder id.",
                "action_taken": False,
            }
    if reminder_id is None and not match:
        return {
            "success": False,
            "message": "Tell me WHICH reminder to cancel (its id or a word from its text).",
            "action_taken": False,
        }

    try:
        from services.database import AsyncSessionLocal
        from services.reminder_service import ReminderService

        async with AsyncSessionLocal() as db:
            service = ReminderService(db)
            # Resolve against the asker's OWN pending list, by id or by text:
            # one ownership rule for both paths, and the confirmation can name
            # what was cancelled.
            pending = await service.list_pending(user_id=user_id, restrict_to_user=True)
            if reminder_id is not None:
                hits = [r for r in pending if r.id == reminder_id]
                label = f"#{reminder_id}"
            else:
                hits = [r for r in pending if match.casefold() in (r.message or "").casefold()]
                label = f"'{match}'"
            if not hits:
                return {
                    "success": False,
                    "message": f"No pending reminder {label} — not found or already fired.",
                    "action_taken": False,
                }
            if len(hits) > 1:
                return {
                    "success": False,
                    "message": (
                        f"{len(hits)} reminders match {label} — nothing cancelled. "
                        "Ask the user which one."
                    ),
                    "action_taken": False,
                    "data": {"candidates": [_describe(r) for r in hits]},
                }
            target = _describe(hits[0])
            ok = await service.cancel(
                target["reminder_id"], user_id=user_id, restrict_to_user=True,
            )

        if not ok:
            # Fired or cancelled between the list and the cancel.
            return {
                "success": False,
                "message": f"Reminder #{target['reminder_id']} already fired or was cancelled.",
                "action_taken": False,
            }
        return {
            "success": True,
            "message": f"Reminder #{target['reminder_id']} cancelled: {target['message']}",
            "action_taken": True,
            "data": target,
        }
    except Exception as e:
        logger.error(f"Error in cancel_reminder tool: {e}")
        return {"success": False, "message": f"Reminder error: {e!s}", "action_taken": False}
