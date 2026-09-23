"""The BOUNDARIES of the shared conversation — where the review found it half-wired.

`test_shared_conversations.py` proves the service layer: reach replaces owner
equality, altering does not follow reach, the atom carries owner and tier. This
file proves the seams around it, each one a defect the review turned up:

* the two browser entry points (WS register, REST fallback) decided on owner
  EQUALITY *before* the service layer ever ran, so a member could see the shared
  thread and never continue it;
* the room history was owned by the first recognised speaker, which §8.1 rules
  out by name — that speaker could then delete the household's thread;
* a room change (`conversation_handoff`) created the target at tier 0, quietly
  turning the shared thread private;
* `DELETE /meetings/{id}/minutes` read-gated a write, so reach let any member
  discard the owner's minutes;
* `enforce_ownership` is an AUTH-ON rule, and a caller passing a hard `True`
  refused every write under auth-off once the check became reach;
* three more places hand a row an owner, and none of them registered its atom.

Real Postgres throughout: every branch of the filter is Postgres SQL.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

# Imported HERE and not inside the tests on purpose: both modules bind
# `AsyncSessionLocal` into their own namespace at import time, and the harness
# rebinds that to the test sessionmaker by sweeping `sys.modules` per test. A
# module first imported INSIDE a test misses the sweep and quietly queries the
# real database — which is how the first cut of these tests "passed": the
# lookups found no row at all and every answer was the default.
import api.websocket.chat_handler as chat_handler
import ha_glue.api.websocket.satellite_handler as satellite_handler
from models.database import ATOM_TYPE_MEETING, Atom, Conversation, Meeting, Message
from services.conversation_service import ConversationService
from tests.backend.dbrows import ensure_role, ensure_speaker, ensure_user

pytestmark = [pytest.mark.backend, pytest.mark.database]


async def _member_of(db: AsyncSession, owner_id: int, member_id: int, tier: int) -> None:
    await db.execute(
        text(
            "INSERT INTO circle_memberships "
            "(circle_owner_id, member_user_id, dimension, value, granted_by, granted_at) "
            "VALUES (:o, :m, 'tier', :v, :o, NOW())"
        ),
        {"o": owner_id, "m": member_id, "v": str(tier)},
    )
    await db.commit()


@pytest.fixture
async def household(db_session: AsyncSession, own_session_to_test_db, monkeypatch):
    """Device account plus two members reaching tier 2, auth ON.

    `own_session_to_test_db` is not optional here: the WS boundary check and
    the device-account lookup open their OWN `AsyncSessionLocal`, and without
    the sweep they would query the real database and answer from an empty
    result — green, and proving nothing.
    """
    from utils.config import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    await ensure_role(db_session)
    device = await ensure_user(db_session, 1, username="haushalt")
    device.is_device_account = True
    anna = await ensure_user(db_session, 2, username="anna")
    bert = await ensure_user(db_session, 3, username="bert")
    await db_session.commit()
    await _member_of(db_session, device.id, anna.id, 2)
    await _member_of(db_session, device.id, bert.id, 2)
    return device, anna, bert


async def _room_thread(db: AsyncSession, device_id: int, session_id: str = "kueche"):
    svc = ConversationService(db)
    await svc.save_message(
        session_id, "user", "wie spät ist es", user_id=device_id, circle_tier=2,
    )
    await db.commit()
    return (await db.execute(
        select(Conversation).where(Conversation.session_id == session_id)
    )).scalar_one()


class TestTheBrowserBoundaries:
    """Both browser entry points run BEFORE the service layer and replace the
    session id. Left on owner equality they made the whole conversion
    unreachable from a browser: read the shared thread, never continue it."""

    async def test_ws_register_follows_reach(self, db_session, household):
        device, anna, _ = household
        await _room_thread(db_session, device.id)

        assert await chat_handler._session_registerable_by("kueche", anna.id) is True

    async def test_ws_register_refuses_an_outsider(self, db_session, household):
        device, _, _ = household
        outsider = await ensure_user(db_session, 9, username="fremd")
        await db_session.commit()
        await _room_thread(db_session, device.id)

        assert await chat_handler._session_registerable_by("kueche", outsider.id) is False

    async def test_ws_register_still_refuses_an_ownerless_row(self, db_session, household):
        """P0 Nr. 5 holds: with adoption gone an ownerless row stays ownerless,
        and every reach branch keys on the owner — so nobody registers for it."""
        _, anna, _ = household
        db_session.add(Conversation(session_id="herrenlos"))
        await db_session.commit()

        assert await chat_handler._session_registerable_by("herrenlos", anna.id) is False

    async def test_ws_register_allows_a_session_with_no_row(self, db_session, household):
        _, anna, _ = household
        assert await chat_handler._session_registerable_by("brandneu", anna.id) is True

    async def test_rest_fallback_follows_reach(self, db_session, household):
        """The REST route the browser uses while the socket is not ready yet."""
        from api.routes.chat import conversation_is_callers

        device, anna, _ = household
        conv = await _room_thread(db_session, device.id)

        assert await conversation_is_callers(conv, anna, db_session) is True

    async def test_rest_fallback_refuses_an_outsider(self, db_session, household):
        from api.routes.chat import conversation_is_callers

        device, _, _ = household
        outsider = await ensure_user(db_session, 9, username="fremd")
        await db_session.commit()
        conv = await _room_thread(db_session, device.id)

        assert await conversation_is_callers(conv, outsider, db_session) is False


class TestWhoOwnsARoomHistory:
    """§8.1: 'Eigentümer immer das Gerätekonto (nie der erste erkannte
    Sprecher)'. The reason is spelled out there: with `chat.own` that speaker
    could delete the shared room history."""

    async def test_the_device_account_owns_it(self, db_session, household, monkeypatch):
        from utils.config import settings

        device, _, _ = household
        monkeypatch.setattr(settings, "satellite_device_account", "haushalt")

        assert await satellite_handler.room_history_owner_id() == device.id

    async def test_no_device_account_means_no_owner(self, db_session, household, monkeypatch):
        """And the caller must then NOT stamp tier 2 — an ownerless row at tier
        2 reaches nobody, so the shared thread would be invisible to everyone."""
        from utils.config import settings

        monkeypatch.setattr(settings, "satellite_device_account", "")

        assert await satellite_handler.room_history_owner_id() is None

    async def test_auth_off_means_no_owner(self, db_session, household, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        monkeypatch.setattr(settings, "satellite_device_account", "haushalt")

        assert await satellite_handler.room_history_owner_id() is None

    async def test_a_member_cannot_delete_the_device_accounts_thread(
        self, db_session, household
    ):
        """The guarantee the ownership rule buys: reach reads, it never wipes."""
        device, anna, _ = household
        conv = await _room_thread(db_session, device.id)

        assert ConversationService.may_alter(conv, anna.id) is False
        assert ConversationService.may_alter(conv, anna.id, caller_has_chat_all=True) is True


class TestARoomChangeKeepsTheReach:
    """`conversation_handoff` runs BEFORE the turn's `save_message`, so whatever
    it creates is what the turn then finds — and `save_message` never revisits
    the tier of a row that already exists."""

    async def test_the_target_inherits_tier_and_atom(self, db_session, household):
        from services import conversation_handoff
        from services.conversation_handoff import try_handoff_context

        conversation_handoff._last_handoff.clear()

        device, _, _ = household
        speaker = await ensure_speaker(db_session, 1, "anna")
        await db_session.commit()

        source = await _room_thread(db_session, device.id, "satellite-kueche")
        source.speaker_id = speaker.id
        await db_session.commit()

        handed = await try_handoff_context(speaker.id, "satellite-bad", db_session)
        assert handed is True
        await db_session.commit()

        target = (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "satellite-bad")
        )).scalar_one()
        assert target.circle_tier == 2, "the room history went private on a room change"
        assert target.atom_id is not None

    async def test_a_member_reaches_the_handed_over_thread(self, db_session, household):
        from services import conversation_handoff
        from services.conversation_handoff import try_handoff_context

        # Module-level per-speaker debounce (10 s) — it survives between tests,
        # so a second handoff for the same speaker id is silently skipped.
        conversation_handoff._last_handoff.clear()

        device, anna, _ = household
        speaker = await ensure_speaker(db_session, 1, "anna")
        await db_session.commit()
        source = await _room_thread(db_session, device.id, "satellite-kueche")
        source.speaker_id = speaker.id
        await db_session.commit()

        await try_handoff_context(speaker.id, "satellite-bad", db_session)
        await db_session.commit()

        target = (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "satellite-bad")
        )).scalar_one()
        assert await ConversationService(db_session).reaches(target.id, anna.id) is True


class TestEveryOwnerAssignmentRegistersAnAtom:
    """'A row gets its atom when it gets an owner' was true in one of four
    places. Owned, tier-bearing and atom-less is the one state that invariant
    promises will not persist: no explicit grant can match it, and
    `AtomService.update_tier` finds nothing to move."""

    async def test_associate_speaker_registers_one(self, db_session, household):
        device, _, _ = household
        speaker = await ensure_speaker(db_session, 1, "anna")
        db_session.add(Conversation(session_id="satellite-flur"))
        await db_session.commit()

        svc = ConversationService(db_session)
        await svc.associate_speaker("satellite-flur", speaker.id, user_id=device.id)

        conv = (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "satellite-flur")
        )).scalar_one()
        assert conv.user_id == device.id
        assert conv.atom_id is not None

    async def test_the_adoption_branch_registers_one(self, db_session, household, monkeypatch):
        """Auth-off adoption — the primary way a legacy row acquires an owner."""
        from utils.config import settings

        monkeypatch.setattr(settings, "auth_enabled", False)
        _, anna, _ = household
        db_session.add(Conversation(session_id="alt"))
        await db_session.commit()

        svc = ConversationService(db_session)
        await svc.save_message("alt", "user", "hallo", user_id=anna.id)
        await db_session.commit()

        conv = (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "alt")
        )).scalar_one()
        assert conv.user_id == anna.id
        assert conv.atom_id is not None

    async def test_a_new_meeting_is_registered(self, db_session, household):
        from api.routes.meetings import _ensure_meeting_atom

        _, anna, _ = household
        meeting = Meeting(owner_user_id=anna.id, status="pending", circle_tier=2)
        db_session.add(meeting)
        await db_session.flush()

        await _ensure_meeting_atom(db_session, meeting)
        await db_session.commit()

        assert meeting.atom_id is not None
        atom = (await db_session.execute(
            select(Atom).where(Atom.atom_id == meeting.atom_id)
        )).scalar_one()
        assert atom.atom_type == ATOM_TYPE_MEETING
        assert atom.source_table == "meetings"
        assert atom.source_id == str(meeting.id)
        assert atom.owner_user_id == anna.id
        assert atom.policy["tier"] == 2

    async def test_an_ownerless_meeting_gets_none(self, db_session):
        """`atoms.owner_user_id` is NOT NULL — auth-off has nobody to name."""
        from api.routes.meetings import _ensure_meeting_atom

        meeting = Meeting(owner_user_id=None, status="pending", circle_tier=2)
        db_session.add(meeting)
        await db_session.flush()

        await _ensure_meeting_atom(db_session, meeting)

        assert meeting.atom_id is None


class TestWritingAMeetingStaysWithTheOwner:
    """Reading a meeting follows reach; every mutator passes `for_write=True`.
    `DELETE /{id}/minutes` did not, and reach then let any member of the
    household discard the owner's minutes."""

    async def test_a_member_reaches_the_meeting(self, db_session, household):
        from api.routes.meetings import _get_owned_meeting

        device, anna, _ = household
        meeting = Meeting(owner_user_id=device.id, status="completed", circle_tier=2)
        db_session.add(meeting)
        await db_session.commit()

        got = await _get_owned_meeting(meeting.id, anna, db_session)
        assert got.id == meeting.id

    async def test_a_member_may_not_write_it(self, db_session, household):
        from api.routes.meetings import _get_owned_meeting

        device, anna, _ = household
        meeting = Meeting(owner_user_id=device.id, status="completed", circle_tier=2)
        db_session.add(meeting)
        await db_session.commit()

        with pytest.raises(HTTPException) as exc:
            await _get_owned_meeting(meeting.id, anna, db_session, for_write=True)
        assert exc.value.status_code == 404

    async def test_discarding_minutes_is_a_write(self, db_session, household):
        """The route the review caught: `DELETE /{id}/minutes` read-gated a
        write, so reach let any member throw away the owner's minutes. (That
        every mutator passes `for_write=True` is walked structurally in
        `test_shared_conversations.py::test_writing_routes_stay_owner_bound`.)"""
        import inspect

        from api.routes.meetings import delete_minutes

        src = inspect.getsource(delete_minutes)
        assert "for_write=True" in src


class TestTheAtomFailureIsSurvivable:
    """`ensure_atom` claims best-effort. It only IS best-effort inside a
    savepoint: `create_with_source` ends in a flush, and a failed flush aborts
    the whole Postgres transaction — the caller's next statement would then die
    with `current transaction is aborted` and the user would lose their message
    anyway, just with a more confusing error."""

    async def test_the_turn_survives_a_failing_atom(self, db_session, household, monkeypatch):
        from services import atom_service

        _, anna, _ = household

        async def boom(self, **kwargs):
            raise RuntimeError("atoms table is having a day")

        monkeypatch.setattr(atom_service.AtomService, "create_with_source", boom)

        svc = ConversationService(db_session)
        msg = await svc.save_message("annas-chat", "user", "trotzdem gespeichert", user_id=anna.id)
        await db_session.commit()

        assert msg is not None
        conv = (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "annas-chat")
        )).scalar_one()
        assert conv.atom_id is None
        # Explicit query, not `conv.messages`: lazy loading on an async session
        # raises MissingGreenlet. The point is that the message SURVIVED the
        # failing atom — the transaction was not poisoned.
        contents = (await db_session.execute(
            select(Message.content).where(Message.conversation_id == conv.id)
        )).scalars().all()
        assert list(contents) == ["trotzdem gespeichert"]


class TestAttachmentsFollowTheThread:
    """An attachment hangs in a conversation; showing the message and 404ing the
    image inside it is a visible break in one and the same thread."""

    async def test_a_member_reaches_an_attachment_in_the_shared_thread(
        self, db_session, household
    ):
        from api.routes.chat_upload import _get_owned_upload
        from models.database import ChatUpload

        device, anna, _ = household
        await _room_thread(db_session, device.id)
        upload = ChatUpload(
            session_id="kueche", filename="zettel.pdf", file_type="pdf",
            file_size=1, file_hash="x", status="completed",
        )
        db_session.add(upload)
        await db_session.commit()

        got = await _get_owned_upload(db_session, upload.id, anna)
        assert got is not None and got.id == upload.id

    async def test_an_outsider_does_not(self, db_session, household):
        from api.routes.chat_upload import _get_owned_upload
        from models.database import ChatUpload

        device, _, _ = household
        outsider = await ensure_user(db_session, 9, username="fremd")
        await _room_thread(db_session, device.id)
        upload = ChatUpload(
            session_id="kueche", filename="zettel.pdf", file_type="pdf",
            file_size=1, file_hash="x", status="completed",
        )
        db_session.add(upload)
        await db_session.commit()

        assert await _get_owned_upload(db_session, upload.id, outsider) is None


class TestEnforceOwnershipIsAnAuthOnRule:
    """The contract `reaches()` relies on. `scanner_jobs` passed a hard `True`
    and got away with it only while the check was `owner != caller` and both
    sides were None under auth-off; under reach a caller without identity fails
    closed, so every scan outcome into a voice-started conversation was
    refused."""

    async def test_every_caller_derives_it_from_the_flag(self):
        import pathlib
        import re

        backend = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"
        offenders: list[str] = []
        for path in backend.rglob("*.py"):
            if path.name in ("conversation_service.py", "ollama_service.py"):
                continue  # the definitions themselves
            for line in path.read_text().splitlines():
                stripped = line.strip()
                if not stripped.startswith("enforce_ownership="):
                    continue
                if not re.match(
                    r"enforce_ownership=(settings\.auth_enabled|enforce_ownership)", stripped
                ):
                    offenders.append(f"{path.relative_to(backend)}: {stripped}")
        assert offenders == [], (
            "ownership enforcement is an auth-on rule; these callers hardcode it: "
            f"{offenders}"
        )
