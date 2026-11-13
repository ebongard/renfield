"""Retention of ``deleted_conversation_sessions`` (#1529).

The tombstones of #1485 were only ever written. What must hold now, against
real Postgres:
- ``CONVERSATION_TOMBSTONE_RETENTION_DAYS`` defaults to 0 = keep forever, and
  a negative value does not start the application;
- with 0 nothing is pruned and NO statement is sent -- by the prune function
  and by the built-in task alike; a negative value reaching the function is
  never read as "everything is expired";
- with N > 0 a record deleted MORE than N days ago is removed, one exactly N
  days old stays (the boundary errs towards keeping), measured on the database
  clock whatever the session's time zone is;
- a record inside the window keeps refusing its id; a pruned one no longer
  does: the id is an unknown one again and whoever sends under it owns a new
  conversation there -- the accepted price of a window, stated here so it is
  a decision;
- an expired record is NOT pruned while rows of the deleted conversation
  survive that a reader hands to the holder of the id -- an attachment
  (``chat_uploads``), an open Paperless confirm, a Paperless filing still in
  flight. Without that guard the deleted conversation's attachments would
  come back with the id, possibly to a different user. The record goes once
  those rows are gone;
- a room id deleted again ages from its LAST delete.

Which tables that is, and that a new session-keyed table cannot be forgotten:
``test_conversation_tombstone_session_keys.py``.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import text

import services.conversation_tombstones as tombstones
from models.database import (
    ChatUpload,
    Conversation,
    PaperlessPendingConfirm,
    PaperlessPendingFinalize,
)
from services.conversation_tombstones import (
    DELETED_AT_INDEX,
    is_deleted_session,
    prune_expired_tombstones,
)
from services.scheduled_tasks import builtins
from utils.config import Settings, settings

pytestmark = [pytest.mark.backend]

_INSERT = (
    "INSERT INTO deleted_conversation_sessions (session_id, deleted_at)"
    # Via text: asyncpg binds an interval parameter as a timedelta only, and
    # the tests want Postgres' own reading of "30 days 1 microsecond".
    " VALUES (:sid, timezone('utc', now()) - CAST(CAST(:age AS text) AS interval))"
)


async def _tombstone(db, session_id: str, age: str, *, commit: bool = True) -> None:
    """A record deleted ``age`` ago (a Postgres interval) on the database clock."""
    await db.execute(text(_INSERT), {"sid": session_id, "age": age})
    if commit:
        await db.commit()


async def _recorded(db) -> list[str]:
    return list((await db.execute(text(
        "SELECT session_id FROM deleted_conversation_sessions ORDER BY session_id"
    ))).scalars().all())


@pytest.mark.unit
class TestTheSetting:
    def test_default_keeps_forever(self):
        assert Settings(_env_file=None).conversation_tombstone_retention_days == 0
        assert Settings.model_fields["conversation_tombstone_retention_days"].default == 0

    def test_a_positive_window_is_accepted(self):
        s = Settings(_env_file=None, conversation_tombstone_retention_days=90)
        assert s.conversation_tombstone_retention_days == 90

    @pytest.mark.parametrize("value", [-1, -365])
    def test_a_negative_window_refuses_boot(self, value):
        with pytest.raises(ValidationError, match="conversation_tombstone_retention_days"):
            Settings(_env_file=None, conversation_tombstone_retention_days=value)


@pytest.mark.unit
class TestKeepForeverSendsNothing:
    @pytest.mark.parametrize("days", [0, -1, -3650])
    async def test_prune_sends_no_statement(self, days):
        db = AsyncMock()

        assert await prune_expired_tombstones(db, days) == 0

        db.execute.assert_not_called()
        db.commit.assert_not_called()

    async def test_the_task_opens_no_session(self, monkeypatch):
        import services.database as database

        def _no_session():
            raise AssertionError("keep-forever must not open a database session")

        monkeypatch.setattr(database, "AsyncSessionLocal", _no_session)
        monkeypatch.setattr(settings, "conversation_tombstone_retention_days", 0)

        out = await builtins._conversation_tombstone_cleanup_handler(SimpleNamespace(), {})

        assert out is not None and out.startswith("skipped")


@pytest.mark.database
class TestThePrune:
    async def test_zero_and_negative_keep_even_ancient_records(self, db_session):
        await _tombstone(db_session, "uralt", "20 years")

        assert await prune_expired_tombstones(db_session, 0) == 0
        assert await prune_expired_tombstones(db_session, -1) == 0

        assert await _recorded(db_session) == ["uralt"]

    async def test_only_records_older_than_the_window_go(self, db_session):
        await _tombstone(db_session, "alt", "31 days")
        await _tombstone(db_session, "jung", "29 days")
        await _tombstone(db_session, "frisch", "0 seconds")

        assert await prune_expired_tombstones(db_session, 30) == 1

        assert await _recorded(db_session) == ["frisch", "jung"]

    async def test_exactly_n_days_old_is_kept_one_microsecond_more_is_not(self, db_session):
        """One transaction: `now()` is the transaction's start time, so the
        insert and the prune see the same instant and the edge is exact."""
        await _tombstone(db_session, "genau", "30 days", commit=False)
        await _tombstone(db_session, "knapp-drueber", "30 days 1 microsecond", commit=False)
        await _tombstone(db_session, "knapp-drunter", "30 days -1 microsecond", commit=False)

        assert await prune_expired_tombstones(db_session, 30) == 1

        assert await _recorded(db_session) == ["genau", "knapp-drunter"]

    @pytest.mark.parametrize("zone", ["Pacific/Kiritimati", "Pacific/Pago_Pago"])
    async def test_the_window_does_not_move_with_the_session_time_zone(self, db_session, zone):
        """`deleted_at` is UTC without a zone. A cut-off taken from a bare
        `now()` would be off by the zone's offset (+14 h / -11 h here) and
        prune early or late."""
        # One transaction: SET LOCAL holds only until the commit in the prune.
        await db_session.execute(text(f"SET LOCAL TIME ZONE '{zone}'"))
        await _tombstone(db_session, "drinnen", "1 day -1 hour", commit=False)
        await _tombstone(db_session, "draussen", "1 day 1 hour", commit=False)

        assert await prune_expired_tombstones(db_session, 1) == 1

        assert await _recorded(db_session) == ["drinnen"]

    async def test_a_record_inside_the_window_still_refuses_its_id(self, db_session):
        """The property #1485 bought must survive the prune for every record
        that has not expired."""
        from api.routes.chat import session_id_is_reusable

        await _tombstone(db_session, "weg-geschuetzt", "89 days")
        await _tombstone(db_session, "weg-abgelaufen", "91 days")

        assert await prune_expired_tombstones(db_session, 90) == 1

        assert await is_deleted_session(db_session, "weg-geschuetzt") is True
        assert await session_id_is_reusable("weg-geschuetzt", None, db_session) is False
        # The price of a window: this id is an unknown one again.
        assert await is_deleted_session(db_session, "weg-abgelaufen") is False
        assert await session_id_is_reusable("weg-abgelaufen", None, db_session) is True

    async def test_a_room_id_deleted_again_ages_from_the_last_delete(self, db_session):
        """The trigger refreshes `deleted_at` on conflict, so an old first
        delete does not expire the record of a recent second one."""
        await _tombstone(db_session, "raum-kueche", "400 days")
        db_session.add(Conversation(session_id="raum-kueche"))
        await db_session.commit()
        await db_session.execute(text("DELETE FROM conversations WHERE session_id = 'raum-kueche'"))
        await db_session.commit()

        assert await prune_expired_tombstones(db_session, 30) == 0

        assert await _recorded(db_session) == ["raum-kueche"]

    async def test_the_model_declares_the_index_the_migration_builds(self, db_session):
        """`create_all` (fresh install, this harness) and the migration must
        agree on the name, or an upgraded and a fresh database differ."""
        rows = (await db_session.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public'"
            " AND tablename = 'deleted_conversation_sessions' AND indexname = :n"
        ), {"n": DELETED_AT_INDEX})).scalars().all()
        assert len(rows) == 1
        assert "(deleted_at)" in rows[0]


async def _upload(db, session_id: str) -> ChatUpload:
    upload = ChatUpload(
        session_id=session_id, filename="rechnung.pdf", status="completed",
        extracted_text="Rechnung 4711",
    )
    db.add(upload)
    await db.commit()
    return upload


async def _remove(db, row) -> None:
    await db.delete(row)
    await db.commit()


@pytest.mark.database
class TestSurvivingRowsKeepTheRecord:
    """The guard of the prune. Every case: a record long past its window."""

    async def test_an_expired_record_with_an_upload_stays_until_the_upload_is_gone(
        self, db_session,
    ):
        from api.routes.chat import session_id_is_reusable

        await _tombstone(db_session, "weg-mit-anhang", "91 days")
        await _tombstone(db_session, "weg-ohne-anhang", "91 days")
        upload = await _upload(db_session, "weg-mit-anhang")

        assert await prune_expired_tombstones(db_session, 90) == 1

        assert await _recorded(db_session) == ["weg-mit-anhang"]
        # So the id stays refused and the attachment stays out of reach.
        assert await session_id_is_reusable("weg-mit-anhang", None, db_session) is False

        await _remove(db_session, upload)

        assert await prune_expired_tombstones(db_session, 90) == 1
        assert await _recorded(db_session) == []

    async def test_an_upload_under_another_id_protects_only_its_own_record(self, db_session):
        await _tombstone(db_session, "weg-a", "91 days")
        await _upload(db_session, "weg-b")

        assert await prune_expired_tombstones(db_session, 90) == 1

        assert await _recorded(db_session) == []

    async def test_an_open_paperless_confirm_keeps_the_record(self, db_session):
        """The confirm row carries its own session id; the attachment it
        points at hangs elsewhere here, so only this guard can hold the record."""
        await _tombstone(db_session, "weg-rueckfrage", "91 days")
        upload = await _upload(db_session, "anderswo")
        confirm = PaperlessPendingConfirm(
            confirm_token="00000000-0000-4000-8000-000000001529",
            attachment_id=upload.id, session_id="weg-rueckfrage",
            llm_output={}, post_fuzzy_output={},
        )
        db_session.add(confirm)
        await db_session.commit()

        assert await prune_expired_tombstones(db_session, 90) == 0
        assert await _recorded(db_session) == ["weg-rueckfrage"]

        await _remove(db_session, confirm)

        assert await prune_expired_tombstones(db_session, 90) == 1
        assert await _recorded(db_session) == []

    async def test_a_filing_in_flight_keeps_the_record_a_finalised_one_does_not(
        self, db_session,
    ):
        from models.database import _utcnow

        await _tombstone(db_session, "weg-ablage", "91 days")
        upload = await _upload(db_session, "anderswo")
        pending = PaperlessPendingFinalize(
            task_id="task-1529", chat_upload_id=upload.id, session_id="weg-ablage",
            filename="rechnung.pdf", deferred_patch={}, original_metadata={},
        )
        db_session.add(pending)
        await db_session.commit()

        assert await prune_expired_tombstones(db_session, 90) == 0
        assert await _recorded(db_session) == ["weg-ablage"]

        pending.finalized_at = _utcnow()
        await db_session.commit()

        assert await prune_expired_tombstones(db_session, 90) == 1
        assert await _recorded(db_session) == []

    async def test_a_surviving_row_does_not_keep_a_record_inside_the_window_from_ageing(
        self, db_session,
    ):
        """The guard only ever keeps; it never prunes by itself."""
        await _tombstone(db_session, "weg-jung", "1 day")
        await _upload(db_session, "weg-jung")

        assert await prune_expired_tombstones(db_session, 90) == 0
        assert await _recorded(db_session) == ["weg-jung"]

    async def test_without_the_guard_the_record_would_go(self, db_session, monkeypatch):
        """Negative control: the guard, and nothing else, keeps the record of
        the first test. With it switched off the same rows are pruned -- the
        state in which the attachment would follow the id to its next owner."""
        await _tombstone(db_session, "weg-mit-anhang", "91 days")
        await _upload(db_session, "weg-mit-anhang")
        monkeypatch.setattr(tombstones, "_prune_guards", lambda: [])

        assert await prune_expired_tombstones(db_session, 90) == 1
        assert await _recorded(db_session) == []

    async def test_the_task_keeps_a_guarded_record(
        self, db_session, own_session_to_test_db, monkeypatch,
    ):
        monkeypatch.setattr(settings, "conversation_tombstone_retention_days", 90)
        await _tombstone(db_session, "weg-mit-anhang", "91 days")
        await _upload(db_session, "weg-mit-anhang")

        assert await builtins._conversation_tombstone_cleanup_handler(SimpleNamespace(), {}) is None
        assert await _recorded(db_session) == ["weg-mit-anhang"]


@pytest.mark.database
class TestTheTask:
    async def test_prunes_with_a_window_set(self, db_session, own_session_to_test_db, monkeypatch):
        monkeypatch.setattr(settings, "conversation_tombstone_retention_days", 30)
        await _tombstone(db_session, "alt", "31 days")
        await _tombstone(db_session, "jung", "1 day")

        out = await builtins._conversation_tombstone_cleanup_handler(SimpleNamespace(), {})

        assert out == "deleted=1"
        assert await _recorded(db_session) == ["jung"]

    async def test_nothing_expired_reports_nothing(
        self, db_session, own_session_to_test_db, monkeypatch,
    ):
        monkeypatch.setattr(settings, "conversation_tombstone_retention_days", 30)
        await _tombstone(db_session, "jung", "1 day")

        assert await builtins._conversation_tombstone_cleanup_handler(SimpleNamespace(), {}) is None
        assert await _recorded(db_session) == ["jung"]

    async def test_keep_forever_leaves_ancient_records(
        self, db_session, own_session_to_test_db, monkeypatch,
    ):
        monkeypatch.setattr(settings, "conversation_tombstone_retention_days", 0)
        await _tombstone(db_session, "uralt", "20 years")

        out = await builtins._conversation_tombstone_cleanup_handler(SimpleNamespace(), {})

        assert out is not None and out.startswith("skipped")
        assert await _recorded(db_session) == ["uralt"]

    def test_the_task_is_seeded_daily_and_not_at_boot(self):
        seed = next(
            s for s in builtins.builtin_task_seeds()
            if s.handler_key == "conversation_tombstone_cleanup"
        )
        assert seed.interval_seconds == 86400
        # A restart must not be what triggers a delete.
        assert seed.run_at_boot is False
