"""A deleted conversation is not revived under its session id (#1485).

The browser mints the session id and keeps it. A conversation deleted
elsewhere (another tab, another device, the admin cleanup, an atom purge) was
recreated under the same id by the next message of a browser still holding it.

What must hold, against real Postgres (the record is written by a trigger):
- every delete path records the id, the FK cascade of an atom purge included;
- the WS boundary and the REST fallback refuse a recorded id that has no row,
  for every caller, and hand out a fresh one;
- a row recreated under a recorded id (a satellite's fixed room id) is judged
  by the reach rule as before;
- a socket that still holds the deleted conversation notices on its next turn;
- an upload made under the deleted id after the delete follows the turn to the
  fresh id; older uploads and other people's uploads stay where they were;
- the row is never recreated where it is written (`save_message`): a turn that
  started before the delete, a scan outcome and a Paperless announcement are
  refused; only the satellite's fixed room id opts in.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

# Imported at module level on purpose: the harness rebinds `AsyncSessionLocal`
# in every module already imported when a test starts (see
# `own_session_to_test_db`); a module first imported inside a test would query
# the real database.
import api.websocket.chat_handler as chat_handler
from models.database import ChatUpload, Conversation, DeletedConversationSession
from services.conversation_service import (
    ConversationDeletedError,
    ConversationNotOwnedError,
    ConversationService,
)
from services.conversation_tombstones import (
    carry_uploads_to_replacement,
    is_deleted_session,
)
from tests.backend.dbrows import ensure_user

pytestmark = [pytest.mark.backend, pytest.mark.database]


class _WS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, msg):
        self.sent.append(msg)


async def _conversation(
    db, session_id: str, user_id: int | None = None, *, recreate: bool = False,
) -> Conversation:
    await ConversationService(db).save_message(
        session_id, "user", "hallo", user_id=user_id, allow_deleted_session=recreate,
    )
    await db.commit()
    return (await db.execute(
        select(Conversation).where(Conversation.session_id == session_id)
    )).scalar_one()


async def _delete(db, session_id: str) -> None:
    conv = (await db.execute(
        select(Conversation).where(Conversation.session_id == session_id)
    )).scalar_one()
    await db.delete(conv)
    await db.commit()


async def _upload(db, session_id: str, uploader_id: int | None, *, created_at=None) -> int:
    upload = ChatUpload(
        session_id=session_id, filename="rechnung.pdf", status="completed",
        uploaded_by_user_id=uploader_id,
    )
    if created_at is not None:
        upload.created_at = created_at
    db.add(upload)
    await db.commit()
    return upload.id


async def _session_of_upload(db, upload_id: int) -> str:
    db.expire_all()
    return (await db.execute(
        select(ChatUpload.session_id).where(ChatUpload.id == upload_id)
    )).scalar_one()


async def _deleted_at(db, session_id: str):
    return (await db.execute(
        select(DeletedConversationSession.deleted_at)
        .where(DeletedConversationSession.session_id == session_id)
    )).scalar_one()


class TestTheRecord:
    async def test_an_orm_delete_records_the_id(self, db_session):
        await _conversation(db_session, "weg-1")
        assert await is_deleted_session(db_session, "weg-1") is False

        await _delete(db_session, "weg-1")

        assert await is_deleted_session(db_session, "weg-1") is True

    async def test_the_record_is_utc_without_a_zone(self, db_session):
        """Compared with `chat_uploads.created_at` (`_utcnow`), so it must be
        UTC whatever the server's time zone is."""
        from models.database import _utcnow

        db_session.add(Conversation(session_id="weg-tz"))
        await db_session.commit()
        # One transaction: the setting holds only for the connection it is on.
        await db_session.execute(text("SET LOCAL TIME ZONE 'Europe/Berlin'"))
        await db_session.execute(text("DELETE FROM conversations WHERE session_id = 'weg-tz'"))
        await db_session.commit()

        deleted_at = await _deleted_at(db_session, "weg-tz")
        assert deleted_at.tzinfo is None
        assert abs(_utcnow() - deleted_at) < timedelta(minutes=5)

    async def test_an_atom_purge_cascade_records_the_id(self, db_session):
        """The purge deletes the conversation through an ON DELETE CASCADE no
        Python code sees -- the reason the record is a trigger."""
        from services.atom_purge_service import AtomPurgeService

        await ensure_user(db_session, 5)
        conv = Conversation(session_id="weg-purge", user_id=5)
        db_session.add(conv)
        await db_session.flush()
        await ConversationService(db_session).ensure_atom(conv)
        await db_session.commit()
        assert conv.atom_id is not None

        await AtomPurgeService.purge(db_session, atom_id=conv.atom_id, reason="test")

        db_session.expire_all()
        assert (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "weg-purge")
        )).scalar_one_or_none() is None
        assert await is_deleted_session(db_session, "weg-purge") is True

    async def test_an_atom_purge_takes_the_messages_with_it(self, db_session):
        """An Art. 17 purge of a conversation that HAS messages: the database
        cascade has to reach the messages, the ORM cascade never runs here."""
        from models.database import Message
        from services.atom_purge_service import AtomPurgeService

        await ensure_user(db_session, 5)
        service = ConversationService(db_session)
        await service.save_message("weg-purge-msgs", "user", "Hallo", user_id=5)
        await service.save_message("weg-purge-msgs", "assistant", "Hallo zurück", user_id=5)
        conv = (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "weg-purge-msgs")
        )).scalar_one()
        await service.ensure_atom(conv)
        await db_session.commit()
        conv_id, atom_id = conv.id, conv.atom_id
        assert atom_id is not None

        await AtomPurgeService.purge(db_session, atom_id=atom_id, reason="test")

        db_session.expire_all()
        assert await db_session.get(Conversation, conv_id) is None
        assert (await db_session.execute(
            select(Message.id).where(Message.conversation_id == conv_id)
        )).first() is None

    async def test_deleting_a_chat_keeps_the_memory_drawn_from_it(self, db_session):
        """A memory points at the message it was drawn from. The chat delete
        removes the message; the memory is an atom of its own and stays, with
        the pointer cleared."""
        from models.database import ConversationMemory

        await ensure_user(db_session, 5)
        msg = await ConversationService(db_session).save_message(
            "weg-mit-erinnerung", "user", "Ich trinke Kaffee schwarz", user_id=5)
        memory = ConversationMemory(
            user_id=5, content="trinkt Kaffee schwarz", source_message_id=msg.id,
            source_session_id="weg-mit-erinnerung",
        )
        db_session.add(memory)
        await db_session.commit()
        memory_id = memory.id
        conv = await db_session.get(Conversation, msg.conversation_id)

        await db_session.delete(conv)
        await db_session.commit()

        db_session.expire_all()
        kept = await db_session.get(ConversationMemory, memory_id)
        assert kept is not None
        assert kept.source_message_id is None

    async def test_a_second_delete_of_the_same_id_moves_the_mark(self, db_session):
        await _conversation(db_session, "weg-2")
        await _delete(db_session, "weg-2")
        await db_session.execute(text(
            "UPDATE deleted_conversation_sessions SET deleted_at = deleted_at - interval '1 day'"
            " WHERE session_id = 'weg-2'"
        ))
        await db_session.commit()
        first = await _deleted_at(db_session, "weg-2")

        await _conversation(db_session, "weg-2", recreate=True)
        await _delete(db_session, "weg-2")

        db_session.expire_all()
        assert await _deleted_at(db_session, "weg-2") > first

    async def test_an_unknown_id_is_not_deleted(self, db_session):
        assert await is_deleted_session(db_session, "nie-gesehen") is False


class TestTheWebSocketBoundary:
    @pytest.mark.parametrize("auth_enabled, caller", [(False, None), (True, 5), (True, None)])
    async def test_a_deleted_id_is_refused_for_every_caller(
        self, db_session, own_session_to_test_db, monkeypatch, auth_enabled, caller,
    ):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", auth_enabled)
        await ensure_user(db_session, 5)
        await db_session.commit()
        await _conversation(db_session, "weg-ws", user_id=5)
        await _delete(db_session, "weg-ws")

        assert await chat_handler._session_registerable_by("weg-ws", caller) is False

    async def test_a_new_id_is_still_free(self, db_session, own_session_to_test_db, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        assert await chat_handler._session_registerable_by("ganz-neu", None) is True

    async def test_a_row_recreated_under_a_deleted_id_follows_the_reach_rule(
        self, db_session, own_session_to_test_db, monkeypatch,
    ):
        """A satellite recreates its room history under the fixed id; that row
        exists, so the deleted mark does not apply to it."""
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        await _conversation(db_session, "kueche")
        await _delete(db_session, "kueche")
        await _conversation(db_session, "kueche", recreate=True)

        assert await chat_handler._session_registerable_by("kueche", None) is True

    async def test_the_replacement_announces_a_fresh_id(
        self, db_session, own_session_to_test_db, monkeypatch,
    ):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        await _conversation(db_session, "weg-neu")
        await _delete(db_session, "weg-neu")
        ws = _WS()

        new_id = await chat_handler._replacement_session_for(ws, "weg-neu", None)

        assert new_id and new_id != "weg-neu"
        assert ws.sent == [{"type": "session_replaced", "session_id": new_id}]
        # The fresh id is not recorded: no replacement loop.
        assert await chat_handler._session_registerable_by(new_id, None) is True

    async def test_a_db_failure_does_not_swap_a_working_session_without_reach(
        self, monkeypatch,
    ):
        """Auth off: the check only guards against reviving a deleted id, and a
        blip must not replace a working conversation with an empty one. Auth
        on with a caller: fail closed, as before."""
        from utils.config import settings

        class _Broken:
            async def __aenter__(self):
                raise RuntimeError("db down")

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(chat_handler, "AsyncSessionLocal", lambda: _Broken())
        monkeypatch.setattr(settings, "auth_enabled", False)
        assert await chat_handler._session_registerable_by("s", None) is True
        monkeypatch.setattr(settings, "auth_enabled", True)
        assert await chat_handler._session_registerable_by("s", 5) is False


class TestALiveSocketNotices:
    """The tab still holds the conversation when it is deleted elsewhere. Its
    history is loaded, so only the per-turn freshness probe can notice."""

    async def test_the_probe_reports_a_deleted_conversation(
        self, db_session, own_session_to_test_db,
    ):
        await _conversation(db_session, "weg-live")
        newest, deleted = await chat_handler._probe_session_freshness("weg-live")
        assert newest is not None and deleted is False

        await _delete(db_session, "weg-live")

        assert await chat_handler._probe_session_freshness("weg-live") == (None, True)

    async def test_the_probe_does_not_call_a_new_session_deleted(
        self, db_session, own_session_to_test_db,
    ):
        assert await chat_handler._probe_session_freshness("noch-leer") == (None, False)

    async def test_a_probe_failure_is_no_measurement(self, monkeypatch):
        class _Broken:
            async def __aenter__(self):
                raise RuntimeError("db down")

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(chat_handler, "AsyncSessionLocal", lambda: _Broken())
        assert await chat_handler._probe_session_freshness("s") == (None, False)


class TestUploadsUnderTheDeletedId:
    async def _deleted(self, db, session_id: str, owner: int | None = None):
        await _conversation(db, session_id, user_id=owner)
        await _delete(db, session_id)

    async def test_an_upload_after_the_delete_follows_the_turn(
        self, db_session, own_session_to_test_db, monkeypatch,
    ):
        """The tab attached a file to its next turn; under auth the attachment
        lookup matches the session, so it must sit under the fresh id."""
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", True)
        await ensure_user(db_session, 5)
        await db_session.commit()
        await self._deleted(db_session, "weg-up", owner=5)
        upload_id = await _upload(db_session, "weg-up", 5)
        ws = _WS()

        new_id = await chat_handler._replacement_session_for(ws, "weg-up", 5)

        assert await _session_of_upload(db_session, upload_id) == new_id

    async def test_an_upload_from_before_the_delete_stays(self, db_session):
        """Moving it would revive part of the deleted conversation."""
        await _conversation(db_session, "weg-alt")
        upload_id = await _upload(db_session, "weg-alt", None)
        await _delete(db_session, "weg-alt")

        moved = await carry_uploads_to_replacement(
            db_session, "weg-alt", "frisch", None, auth_enabled=False,
        )

        assert moved == 0
        assert await _session_of_upload(db_session, upload_id) == "weg-alt"

    async def test_under_auth_only_the_callers_own_uploads_move(self, db_session):
        await ensure_user(db_session, 5)
        await ensure_user(db_session, 6)
        await db_session.commit()
        await self._deleted(db_session, "weg-zwei", owner=5)
        mine = await _upload(db_session, "weg-zwei", 5)
        theirs = await _upload(db_session, "weg-zwei", 6)

        moved = await carry_uploads_to_replacement(
            db_session, "weg-zwei", "frisch", 5, auth_enabled=True,
        )

        assert moved == 1
        assert await _session_of_upload(db_session, mine) == "frisch"
        assert await _session_of_upload(db_session, theirs) == "weg-zwei"

    async def test_under_auth_no_identity_moves_nothing(self, db_session):
        await self._deleted(db_session, "weg-anon")
        upload_id = await _upload(db_session, "weg-anon", None)

        assert await carry_uploads_to_replacement(
            db_session, "weg-anon", "frisch", None, auth_enabled=True,
        ) == 0
        assert await _session_of_upload(db_session, upload_id) == "weg-anon"

    async def test_auth_off_moves_every_upload_made_since_the_delete(self, db_session):
        await self._deleted(db_session, "weg-off")
        upload_id = await _upload(db_session, "weg-off", None)

        assert await carry_uploads_to_replacement(
            db_session, "weg-off", "frisch", None, auth_enabled=False,
        ) == 1
        assert await _session_of_upload(db_session, upload_id) == "frisch"

    async def test_a_foreign_session_keeps_its_uploads(
        self, db_session, own_session_to_test_db, monkeypatch,
    ):
        """The replacement for a session that is not the caller's must not
        take any of its uploads along -- it was never deleted."""
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", True)
        await ensure_user(db_session, 5)
        await ensure_user(db_session, 6)
        await db_session.commit()
        await _conversation(db_session, "fremd", user_id=6)
        upload_id = await _upload(db_session, "fremd", 5)

        new_id = await chat_handler._replacement_session_for(_WS(), "fremd", 5)

        assert new_id is not None
        assert await _session_of_upload(db_session, upload_id) == "fremd"

    async def test_a_failing_move_does_not_fail_the_turn(self, monkeypatch):
        import services.conversation_tombstones as tombstones

        monkeypatch.setattr(
            tombstones, "carry_uploads_to_replacement",
            AsyncMock(side_effect=RuntimeError("db down")),
        )
        await chat_handler._carry_uploads_to_replacement("a", "b", 5)


class TestTheRestFallback:
    async def test_a_deleted_id_is_not_reusable(self, db_session):
        from api.routes.chat import session_id_is_reusable

        await _conversation(db_session, "weg-rest")
        await _delete(db_session, "weg-rest")

        assert await session_id_is_reusable("weg-rest", None, db_session) is False
        assert await session_id_is_reusable("ganz-neu", None, db_session) is True

    async def test_an_existing_row_is_reusable(self, db_session):
        from api.routes.chat import session_id_is_reusable

        conv = await _conversation(db_session, "da")
        assert await session_id_is_reusable("da", conv, db_session) is True

    async def test_send_answers_under_a_fresh_id(self, async_client, db_session, monkeypatch):
        """The browser's fallback while the socket is not up: the answer
        carries the id actually used, and nothing exists under the deleted one."""
        from main import app
        from utils.config import settings

        monkeypatch.setattr(settings, "agent_enabled", False)
        ollama = AsyncMock()
        ollama.extract_ranked_intents = AsyncMock(
            return_value=[{"intent": "general.conversation", "parameters": {}}]
        )
        ollama.chat = AsyncMock(return_value="Hallo.")
        monkeypatch.setattr(app.state, "ollama", ollama, raising=False)
        await _conversation(db_session, "weg-send")
        await _delete(db_session, "weg-send")

        r = await async_client.post(
            "/api/chat/send", json={"message": "Hallo", "session_id": "weg-send"},
        )

        assert r.status_code == 200, r.text
        new_id = r.json()["session_id"]
        assert new_id != "weg-send"
        db_session.expire_all()
        assert (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "weg-send")
        )).scalar_one_or_none() is None
        assert (await db_session.execute(
            select(Conversation).where(Conversation.session_id == new_id)
        )).scalar_one_or_none() is not None


class _Ollama:
    """The one `ollama` method the replacement path calls, on the real service."""

    async def save_message(self, session_id, role, content, db, **kw):
        return await ConversationService(db).save_message(
            session_id, role, content, kw.pop("metadata", None), **kw,
        )


async def _row(db, session_id: str):
    db.expire_all()
    return (await db.execute(
        select(Conversation).where(Conversation.session_id == session_id)
    )).scalar_one_or_none()


class TestTheWriterRefusesADeletedId:
    """The row is created in `save_message`, so the record is enforced there
    too -- not only at the chat boundary, which a turn already past it, a scan
    outcome or a Paperless announcement never crosses again."""

    async def test_save_message_refuses_to_recreate_a_deleted_conversation(self, db_session):
        await _conversation(db_session, "weg-save")
        await _delete(db_session, "weg-save")

        with pytest.raises(ConversationDeletedError) as raised:
            await ConversationService(db_session).save_message("weg-save", "user", "noch da?")

        # Every writer already handles the parent class correctly.
        assert isinstance(raised.value, ConversationNotOwnedError)
        assert await _row(db_session, "weg-save") is None

    async def test_the_satellite_opt_in_recreates_its_room_id(self, db_session):
        await _conversation(db_session, "satellite-kueche")
        await _delete(db_session, "satellite-kueche")

        await ConversationService(db_session).save_message(
            "satellite-kueche", "user", "Licht an", allow_deleted_session=True,
        )
        # Once the row exists again, plain appends work as before.
        await ConversationService(db_session).save_message(
            "satellite-kueche", "assistant", "Erledigt.",
        )

        assert await _row(db_session, "satellite-kueche") is not None

    async def test_the_satellite_handler_opts_in(self):
        """Pinned on the call itself: without the flag a deleted room history
        would silently stop being written for the rest of the day."""
        import ast
        import inspect

        from ha_glue.api.websocket import satellite_handler

        tree = ast.parse(inspect.getsource(satellite_handler))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "save_message"
        ]
        assert calls
        for call in calls:
            flags = {kw.arg: kw.value for kw in call.keywords}
            assert isinstance(flags.get("allow_deleted_session"), ast.Constant)
            assert flags["allow_deleted_session"].value is True

    @pytest.mark.parametrize("auth_enabled, caller", [(False, None), (True, 5)])
    async def test_a_turn_whose_conversation_was_deleted_mid_turn_lands_in_a_fresh_one(
        self, db_session, own_session_to_test_db, monkeypatch, auth_enabled, caller,
    ):
        """The probe runs at turn START, the save after the LLM round. A delete
        in between must not be revived by the save: it raises, and the
        handler's replacement path opens a fresh conversation -- under auth-off
        too, where the turn has no identity."""
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", auth_enabled)
        await ensure_user(db_session, 5)
        await db_session.commit()
        await _conversation(db_session, "weg-mitten", user_id=caller)
        # Turn start: the conversation is there, nothing to swap.
        newest, deleted = await chat_handler._probe_session_freshness("weg-mitten")
        assert newest is not None and deleted is False

        await _delete(db_session, "weg-mitten")  # another tab, mid-turn

        ollama = _Ollama()
        with pytest.raises(ConversationNotOwnedError):  # what the handler catches
            await ollama.save_message(
                "weg-mitten", "user", "Frage", db_session,
                user_id=caller, enforce_ownership=auth_enabled,
            )
        ws = _WS()
        new_id, user_msg_id, asst_msg_id = await chat_handler._restart_conversation_for_caller(
            ws, db_session, ollama, user_id=caller, content="Frage", response="Antwort",
            user_metadata=None, assistant_metadata=None,
        )

        assert new_id and new_id != "weg-mitten"
        assert user_msg_id and asst_msg_id
        assert ws.sent == [{"type": "session_replaced", "session_id": new_id}]
        assert await _row(db_session, "weg-mitten") is None
        assert (await _row(db_session, new_id)).user_id == caller

    async def test_under_auth_a_turn_without_identity_still_opens_nothing(
        self, db_session, monkeypatch,
    ):
        """Unchanged: under auth a replacement without an owner would mint one
        ownerless row per turn."""
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", True)
        ws = _WS()
        assert await chat_handler._restart_conversation_for_caller(
            ws, db_session, _Ollama(), user_id=None, content="Frage", response="Antwort",
            user_metadata=None, assistant_metadata=None,
        ) == (None, None, None)
        assert ws.sent == []

    async def test_a_scan_outcome_after_the_delete_is_refused(
        self, db_session, monkeypatch,
    ):
        """The scan started in the conversation; it was deleted before the
        scanner reported. The outcome settles as refused and writes nothing."""
        from services import scanner_jobs as sj
        from tests.backend.test_scanner_jobs import JOB_ID, FakeRedis, _event, _tool_result
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        await _conversation(db_session, "weg-scan")
        redis = FakeRedis()
        await sj.remember_scan_requester(
            _tool_result({"ok": True, "job_id": JOB_ID}), user_id=None,
            session_id="weg-scan", redis=redis,
        )
        await _delete(db_session, "weg-scan")

        assert await sj.handle_job_event(db_session, _event(), redis=redis) == "refused"
        assert await _row(db_session, "weg-scan") is None
        assert redis.published == []
        # Final, like any refusal: the retry is a duplicate, not a second try.
        assert await sj.handle_job_event(db_session, _event(), redis=redis) == "duplicate"
