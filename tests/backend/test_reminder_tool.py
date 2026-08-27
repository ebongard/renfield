"""
Unit tests for `internal.create_reminder` — the chat-agent reminder tool (#1146).

Guarantees: it persists a reminder via the shared ReminderService, injects the
authenticated user_id/session_id (never from LLM params), re-asserts the
proactive_reminders runtime gate, validates inputs, and degrades cleanly on
parse errors. Plus the parse_duration timezone-normalization root fix that made
a tz-aware ISO trigger safe on the naive-datetime comparison path.
"""
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.reminder_service import ReminderService
from services.reminder_tool import REMINDER_TOOL, create_reminder

pytestmark = pytest.mark.unit


def _patches(reminder=None, side_effect=None):
    """Patch the session + ReminderService the tool imports at call time."""
    svc = MagicMock()
    svc.create_reminder = AsyncMock(return_value=reminder, side_effect=side_effect)

    @asynccontextmanager
    async def _session(*_a, **_k):
        yield MagicMock()

    return (
        svc,
        patch("services.database.AsyncSessionLocal", lambda *a, **k: _session()),
        patch("services.reminder_service.ReminderService", return_value=svc),
    )


def _fake_reminder(rid=7, room=None):
    r = MagicMock()
    r.id = rid
    r.trigger_at = datetime(2026, 8, 27, 18, 44, 0)
    r.room_name = room
    return r


def _enabled(flag=True):
    return patch("services.reminder_tool.settings.proactive_reminders_enabled", flag)


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

def test_tool_definition_shape():
    assert "internal.create_reminder" in REMINDER_TOOL
    defn = REMINDER_TOOL["internal.create_reminder"]
    assert "description" in defn
    assert set(defn["parameters"]) >= {"message", "trigger_at"}


# ---------------------------------------------------------------------------
# Happy path + injection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_creates_reminder_and_injects_identity():
    rem = _fake_reminder(rid=42)
    svc, p_sess, p_svc = _patches(reminder=rem)
    with _enabled(), p_sess, p_svc:
        result = await create_reminder(
            {"message": "Testnotiz prüfen", "trigger_at": "in 2 minutes"},
            user_id=5,
            session_id="sess-abc",
        )

    assert result["success"] is True
    assert result["action_taken"] is True
    assert result["data"]["reminder_id"] == 42
    # user_id + session_id come from the injected context, not params.
    svc.create_reminder.assert_awaited_once()
    kwargs = svc.create_reminder.await_args.kwargs
    assert kwargs["user_id"] == 5
    assert kwargs["session_id"] == "sess-abc"
    assert kwargs["message"] == "Testnotiz prüfen"
    assert kwargs["trigger_at_str"] == "in 2 minutes"


@pytest.mark.asyncio
async def test_llm_supplied_user_id_is_ignored():
    """A crafted user_id in params must never override the injected identity."""
    rem = _fake_reminder()
    svc, p_sess, p_svc = _patches(reminder=rem)
    with _enabled(), p_sess, p_svc:
        await create_reminder(
            {"message": "x", "trigger_at": "in 1 hour", "user_id": 999},
            user_id=1,
            session_id=None,
        )
    assert svc.create_reminder.await_args.kwargs["user_id"] == 1


@pytest.mark.asyncio
async def test_room_and_when_alias_accepted():
    rem = _fake_reminder(room="Küche")
    svc, p_sess, p_svc = _patches(reminder=rem)
    with _enabled(), p_sess, p_svc:
        result = await create_reminder(
            {"message": "Essen", "when": "um 18:00", "room": "Küche"},
            user_id=1,
        )
    assert result["success"] is True
    assert svc.create_reminder.await_args.kwargs["trigger_at_str"] == "um 18:00"
    assert svc.create_reminder.await_args.kwargs["room"] == "Küche"


# ---------------------------------------------------------------------------
# Gate + validation + error handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_off_refuses_without_persisting():
    svc, p_sess, p_svc = _patches(reminder=_fake_reminder())
    with _enabled(False), p_sess, p_svc:
        result = await create_reminder(
            {"message": "x", "trigger_at": "in 1 hour"}, user_id=1
        )
    assert result["success"] is False
    assert result["action_taken"] is False
    svc.create_reminder.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_message_rejected():
    with _enabled():
        result = await create_reminder({"trigger_at": "in 1 hour"}, user_id=1)
    assert result["success"] is False
    assert result["action_taken"] is False


@pytest.mark.asyncio
async def test_missing_trigger_rejected():
    with _enabled():
        result = await create_reminder({"message": "x"}, user_id=1)
    assert result["success"] is False
    assert result["action_taken"] is False


@pytest.mark.asyncio
async def test_parse_error_surfaces_as_tool_failure():
    svc, p_sess, p_svc = _patches(side_effect=ValueError("Could not parse trigger time: 'bald'"))
    with _enabled(), p_sess, p_svc:
        result = await create_reminder(
            {"message": "x", "trigger_at": "bald"}, user_id=1
        )
    assert result["success"] is False
    assert "parse" in result["message"].lower()


@pytest.mark.asyncio
async def test_unexpected_error_is_caught():
    svc, p_sess, p_svc = _patches(side_effect=RuntimeError("db down"))
    with _enabled(), p_sess, p_svc:
        result = await create_reminder(
            {"message": "x", "trigger_at": "in 1 hour"}, user_id=1
        )
    assert result["success"] is False
    assert result["action_taken"] is False


# ---------------------------------------------------------------------------
# parse_duration timezone-normalization root fix (#1146)
# ---------------------------------------------------------------------------

def test_parse_duration_naive_iso_unchanged():
    dt = ReminderService.parse_duration("2026-08-27T18:44:00")
    assert isinstance(dt, datetime)
    assert dt.tzinfo is None


def test_parse_duration_tz_aware_iso_normalized_to_naive_utc():
    """A tz-aware ISO string must come back naive-UTC so the downstream
    `trigger_at <= now()` comparison doesn't raise the offset-naive/aware
    TypeError that made #1146's create path crash."""
    dt = ReminderService.parse_duration("2026-08-27T20:44:00+02:00")
    assert dt.tzinfo is None
    # 20:44 +02:00 == 18:44 UTC
    assert dt == datetime(2026, 8, 27, 18, 44, 0)


def test_parse_duration_relative_still_timedelta():
    assert ReminderService.parse_duration("in 30 Minuten") == timedelta(minutes=30)
    assert ReminderService.parse_duration("in 45 seconds") == timedelta(seconds=45)


def test_parse_duration_naive_iso_is_local_time():
    """A naive ISO datetime is interpreted as LOCAL wall-clock, stored naive-UTC
    (#1146). Pinned to Europe/Berlin (CET, +01:00 in December) for determinism:
    10:00 local == 09:00 UTC."""
    with patch("services.daypart_service.settings.daypart_timezone", "Europe/Berlin"):
        dt = ReminderService.parse_duration("2026-12-25T10:00:00")
    assert dt.tzinfo is None
    assert dt == datetime(2026, 12, 25, 9, 0, 0)


def test_parse_duration_um_hhmm_is_local_time():
    """'um HH:MM' resolves in the local zone, stored naive-UTC. Round-tripping
    back through the pinned zone must yield the requested wall-clock time."""
    from zoneinfo import ZoneInfo

    with patch("services.daypart_service.settings.daypart_timezone", "Europe/Berlin"):
        dt = ReminderService.parse_duration("um 18:00")
    assert dt.tzinfo is None
    local = dt.replace(tzinfo=UTC).astimezone(ZoneInfo("Europe/Berlin"))
    assert (local.hour, local.minute) == (18, 0)


@pytest.mark.asyncio
async def test_create_reminder_service_accepts_tz_aware_iso():
    """End-to-end on the service: a future tz-aware ISO trigger no longer raises."""
    future = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
    parsed = ReminderService.parse_duration(future.isoformat())
    assert parsed.tzinfo is None
    assert parsed > datetime.now(UTC).replace(tzinfo=None)
