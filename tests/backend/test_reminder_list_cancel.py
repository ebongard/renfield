"""`internal.list_reminders` / `internal.cancel_reminder` — reminders by voice (#264).

"Welche Timer laufen?" and "brich den Nudel-Timer ab" had no agent path: the
list and the cancel existed only as REST routes for the web UI. Guarantees:

- through the dispatch, on real Postgres: the asker sees and cancels only their
  OWN pending reminders — a smuggled `user_id` or another user's id changes
  nothing, and an admin is scoped like anyone else;
- an ambiguous text match cancels nothing and returns the candidates;
- an unidentified turn reaches only ownerless reminders (they fire publicly);
- every time the agent sees is LOCAL time, never the stored naive UTC — the
  create tool used to confirm "um 18:00" as "16:00" for a CEST user;
- the runtime gate is re-asserted in every handler (H4).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from models.database import REMINDER_CANCELLED, REMINDER_PENDING, Reminder
from services.reminder_service import ReminderService, naive_utc_to_local
from services.reminder_tool import (
    REMINDER_TOOL,
    cancel_reminder,
    create_reminder,
    list_reminders,
)
from tests.backend.dbrows import ensure_role, ensure_user
from utils.config import settings

BERLIN = ZoneInfo("Europe/Berlin")


def _berlin():
    return patch("services.daypart_service._resolve_tz", return_value=BERLIN)


async def _call(intent: str, asker_id: int | None, **params) -> dict:
    from services.action_executor import ActionExecutor

    return await ActionExecutor().execute(
        {"intent": intent, "parameters": params, "confidence": 1.0},
        user_id=asker_id,
    )


def _in(minutes: int) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=minutes)


@pytest.fixture
async def reminders(db_session, monkeypatch):
    """alice: oven + pasta; bob: oven; one ownerless — all pending."""
    monkeypatch.setattr(settings, "proactive_reminders_enabled", True)
    monkeypatch.setattr(settings, "auth_enabled", True)
    await ensure_role(db_session)
    alice = await ensure_user(db_session, 1, username="alice")
    bob = await ensure_user(db_session, 2, username="bob")
    rows = {
        "alice_oven": Reminder(message="Ofen ausschalten", trigger_at=_in(20), user_id=alice.id),
        "alice_pasta": Reminder(message="Nudeln abgießen", trigger_at=_in(12), user_id=alice.id),
        "bob_oven": Reminder(message="Ofen vorheizen", trigger_at=_in(30), user_id=bob.id),
        "nobody": Reminder(message="Wäsche aufhängen", trigger_at=_in(40), user_id=None),
    }
    for r in rows.values():
        r.status = REMINDER_PENDING
        db_session.add(r)
    await db_session.commit()
    # Plain ids: the rows expire on every commit the tools make.
    return alice.id, bob.id, {k: r.id for k, r in rows.items()}


async def _status(db_session, reminder_id: int) -> str:
    db_session.expire_all()
    return (await db_session.execute(
        select(Reminder.status).where(Reminder.id == reminder_id)
    )).scalar_one()


# ------------------------------------------------- through the dispatch ------
@pytest.mark.database
class TestListThroughTheDispatch:

    async def test_lists_only_the_askers_own_pending_reminders(
        self, db_session, reminders, own_session_to_test_db
    ):
        alice, bob, rows = reminders
        listed = await _call("internal.list_reminders", alice, user_id=bob)

        assert listed["success"] is True, listed
        ids = [r["reminder_id"] for r in listed["data"]["reminders"]]
        # Soonest first, bob's and the ownerless one absent despite the smuggled id.
        assert ids == [rows["alice_pasta"], rows["alice_oven"]]

    async def test_an_admin_is_scoped_to_their_own_reminders_too(
        self, db_session, reminders, own_session_to_test_db
    ):
        """Unlike `GET /api/reminders`: a spoken 'my timers' means one's own."""
        _alice, _bob, _rows = reminders
        admin = await ensure_user(
            db_session, 3, username="chef", role_name="Admin", permissions=["admin"],
        )
        await db_session.commit()

        listed = await _call("internal.list_reminders", admin.id)
        assert listed["success"] is True, listed
        assert listed["data"]["reminders"] == [], "an admin listed other users' reminders"

    async def test_an_unidentified_turn_sees_only_ownerless_reminders(
        self, db_session, reminders, own_session_to_test_db
    ):
        _alice, _bob, rows = reminders
        listed = await _call("internal.list_reminders", None)
        assert [r["reminder_id"] for r in listed["data"]["reminders"]] == [rows["nobody"]]


@pytest.mark.database
class TestCancelThroughTheDispatch:

    async def test_cancels_the_askers_reminder_by_text(
        self, db_session, reminders, own_session_to_test_db
    ):
        alice, _bob, rows = reminders
        result = await _call("internal.cancel_reminder", alice, match="nudel")

        assert result["success"] is True, result
        assert result["action_taken"] is True
        assert result["data"]["reminder_id"] == rows["alice_pasta"]
        assert await _status(db_session, rows["alice_pasta"]) == REMINDER_CANCELLED
        assert await _status(db_session, rows["alice_oven"]) == REMINDER_PENDING

    async def test_another_users_reminder_cannot_be_cancelled_by_id(
        self, db_session, reminders, own_session_to_test_db
    ):
        alice, bob, rows = reminders
        result = await _call(
            "internal.cancel_reminder", alice,
            reminder_id=rows["bob_oven"], user_id=bob,
        )

        assert result["success"] is False, result
        assert result["action_taken"] is False
        assert await _status(db_session, rows["bob_oven"]) == REMINDER_PENDING

    async def test_a_text_match_never_reaches_another_users_reminder(
        self, db_session, reminders, own_session_to_test_db
    ):
        """'Ofen' matches alice's AND bob's — for alice it is unambiguous."""
        alice, _bob, rows = reminders
        result = await _call("internal.cancel_reminder", alice, match="Ofen")

        assert result["success"] is True, result
        assert result["data"]["reminder_id"] == rows["alice_oven"]
        assert await _status(db_session, rows["bob_oven"]) == REMINDER_PENDING

    async def test_an_ambiguous_match_cancels_nothing_and_returns_candidates(
        self, db_session, reminders, own_session_to_test_db
    ):
        alice, _bob, rows = reminders
        db_session.add(Reminder(
            message="Ofen reinigen", trigger_at=_in(50), user_id=alice,
            status=REMINDER_PENDING,
        ))
        await db_session.commit()

        result = await _call("internal.cancel_reminder", alice, match="ofen")

        assert result["success"] is False, result
        assert result["action_taken"] is False
        assert len(result["data"]["candidates"]) == 2
        assert await _status(db_session, rows["alice_oven"]) == REMINDER_PENDING

    async def test_by_id_confirms_what_was_cancelled(
        self, db_session, reminders, own_session_to_test_db
    ):
        alice, _bob, rows = reminders
        result = await _call(
            "internal.cancel_reminder", alice, reminder_id=f"#{rows['alice_oven']}",
        )

        assert result["success"] is True, result
        assert "Ofen ausschalten" in result["message"]
        assert await _status(db_session, rows["alice_oven"]) == REMINDER_CANCELLED

    async def test_an_unidentified_turn_cannot_cancel_an_owned_reminder(
        self, db_session, reminders, own_session_to_test_db
    ):
        _alice, _bob, rows = reminders
        result = await _call(
            "internal.cancel_reminder", None, reminder_id=rows["alice_oven"],
        )

        assert result["success"] is False, result
        assert await _status(db_session, rows["alice_oven"]) == REMINDER_PENDING


# ------------------------------------------------------------- unit ----------
def _session_with(svc):
    @asynccontextmanager
    async def _session(*_a, **_k):
        yield MagicMock()

    return (
        patch("services.database.AsyncSessionLocal", lambda *a, **k: _session()),
        patch("services.reminder_service.ReminderService", return_value=svc),
    )


def _row(rid, message, trigger_at):
    r = MagicMock()
    r.id, r.message, r.trigger_at, r.room_name = rid, message, trigger_at, None
    return r


@pytest.mark.unit
def test_both_tools_are_advertised_without_identity_parameters():
    for name in ("internal.list_reminders", "internal.cancel_reminder"):
        params = REMINDER_TOOL[name]["parameters"]
        assert not {"user_id", "session_id"} & set(params), name


@pytest.mark.unit
@pytest.mark.parametrize("handler,params", [
    (list_reminders, {}),
    (cancel_reminder, {"match": "Ofen"}),
])
async def test_gate_off_refuses_without_touching_the_database(monkeypatch, handler, params):
    monkeypatch.setattr(settings, "proactive_reminders_enabled", False)
    with patch("services.database.AsyncSessionLocal") as session:
        result = await handler(params, user_id=1)
    assert result["success"] is False
    session.assert_not_called()


@pytest.mark.unit
async def test_listed_times_are_local_not_stored_utc(monkeypatch):
    monkeypatch.setattr(settings, "proactive_reminders_enabled", True)
    svc = MagicMock()
    # 16:00 naive UTC on a CEST date == 18:00 in Berlin.
    svc.list_pending = AsyncMock(return_value=[_row(9, "Ofen", datetime(2026, 8, 27, 16, 0))])
    p_sess, p_svc = _session_with(svc)
    with _berlin(), p_sess, p_svc:
        result = await list_reminders({}, user_id=1)

    assert result["data"]["reminders"][0]["trigger_at"] == "2026-08-27T18:00+02:00"
    assert "18:00" in result["message"] and "16:00" not in result["message"]


@pytest.mark.unit
async def test_create_confirms_in_local_time(monkeypatch):
    monkeypatch.setattr(settings, "proactive_reminders_enabled", True)
    svc = MagicMock()
    svc.create_reminder = AsyncMock(return_value=_row(4, "Ofen", datetime(2026, 8, 27, 16, 0)))
    p_sess, p_svc = _session_with(svc)
    with _berlin(), p_sess, p_svc:
        result = await create_reminder({"message": "Ofen", "trigger_at": "um 18:00"}, user_id=1)

    assert "2026-08-27 18:00" in result["message"], result["message"]
    assert result["data"]["trigger_at"] == "2026-08-27T18:00+02:00"


@pytest.mark.unit
@pytest.mark.parametrize("params", [{}, {"match": "  "}, {"reminder_id": "abc"}])
async def test_cancel_without_a_usable_target_is_refused(monkeypatch, params):
    monkeypatch.setattr(settings, "proactive_reminders_enabled", True)
    with patch("services.database.AsyncSessionLocal") as session:
        result = await cancel_reminder(params, user_id=1)
    assert result["success"] is False
    assert result["action_taken"] is False
    session.assert_not_called()


@pytest.mark.unit
def test_utc_to_local_round_trips_with_local_to_utc():
    with _berlin():
        local = datetime(2026, 1, 15, 7, 30)
        utc = ReminderService._naive_local_to_naive_utc(local)
        assert naive_utc_to_local(utc).replace(tzinfo=None) == local


@pytest.mark.unit
@pytest.mark.parametrize("raw", [5, "5", "#5", 5.0, "5.0"])
async def test_cancel_accepts_every_spelling_of_an_id(monkeypatch, raw):
    monkeypatch.setattr(settings, "proactive_reminders_enabled", True)
    svc = MagicMock()
    svc.list_pending = AsyncMock(return_value=[_row(5, "Ofen", datetime(2026, 8, 27, 16, 0))])
    svc.cancel = AsyncMock(return_value=True)
    p_sess, p_svc = _session_with(svc)
    with _berlin(), p_sess, p_svc:
        result = await cancel_reminder({"reminder_id": raw}, user_id=1)
    assert result["success"] is True, result
    assert svc.cancel.await_args.args[0] == 5


@pytest.mark.unit
async def test_a_fractional_id_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "proactive_reminders_enabled", True)
    with patch("services.database.AsyncSessionLocal") as session:
        result = await cancel_reminder({"reminder_id": 5.5}, user_id=1)
    assert result["success"] is False
    session.assert_not_called()
