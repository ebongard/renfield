"""A conversation is a shared artifact — reach decides, not owner equality.

`docs/design/household-auth-on-cutover.md` §8.1: a kitchen is a multi-person
room. A asks at the satellite, B follows up, and that is ONE thread. With
ownership as equality B's follow-up either failed or opened a context-less new
conversation, and no member saw the room history in their list.

So a conversation carries a tier and an atom, and every read path runs the same
four-branch `circle_sql` filter the rest of the system uses. Destructive acts do
NOT follow reach: deleting a shared room history stays with its owner (the
device account) plus `chat.all`, the admin.

Everything here runs against real Postgres — the filter is Postgres SQL, and
the reach branches join `circle_memberships`.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ATOM_TYPE_CONVERSATION, Atom, Conversation
from services.conversation_service import ConversationNotOwnedError, ConversationService
from tests.backend.dbrows import ensure_role, ensure_user

pytestmark = [pytest.mark.backend, pytest.mark.database]


async def _member_of(db: AsyncSession, owner_id: int, member_id: int, tier: int) -> None:
    """Give `member` reach into `owner`'s circles at `tier`.

    The tier-reach branch reads `circle_memberships` (dimension 'tier'), and a
    member reaches an atom whose tier is at or above their own depth.
    """
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
async def household(db_session: AsyncSession, monkeypatch):
    """A device account and two members, the members reaching tier 2.

    `AUTH_ENABLED=true` for the whole file: this is the auth-on world. The
    list and the message search read the flag directly (they take a `user_id`
    but no `enforce_ownership` switch), so without it they would fall back to
    owner equality — which is exactly today's single-trust-domain behaviour
    and not what is under test here.
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


class TestReachReplacesOwnerEquality:
    async def test_a_member_reaches_the_rooms_history(self, db_session, household):
        """The point of the whole item: the kitchen thread belongs to the device
        account, and Anna — who owns nothing here — can read and continue it."""
        device, anna, _ = household
        svc = ConversationService(db_session)
        await svc.save_message(
            "kueche", "user", "wie spät ist es", user_id=device.id, circle_tier=2,
        )
        await db_session.commit()

        conv = (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "kueche")
        )).scalar_one()

        assert await svc.reaches(conv.id, anna.id) is True
        ctx = await svc.load_context("kueche", user_id=anna.id, enforce_ownership=True)
        assert [m["content"] for m in ctx] == ["wie spät ist es"]

    async def test_a_member_may_CONTINUE_the_shared_thread(self, db_session, household):
        """B's follow-up lands in the SAME conversation — that is the difference
        between one kitchen thread and a context-less new one per person."""
        device, anna, _ = household
        svc = ConversationService(db_session)
        await svc.save_message(
            "kueche", "user", "A fragt", user_id=device.id, circle_tier=2,
        )
        await db_session.commit()

        await svc.save_message(
            "kueche", "user", "B hakt nach", user_id=anna.id, enforce_ownership=True,
        )
        await db_session.commit()

        ctx = await svc.load_context("kueche", user_id=anna.id, enforce_ownership=True)
        assert [m["content"] for m in ctx] == ["A fragt", "B hakt nach"]

    async def test_an_outsider_reaches_nothing(self, db_session, household):
        """No membership, no reach — a tier-2 row is not public."""
        device, _, _ = household
        outsider = await ensure_user(db_session, 9, username="fremd")
        await db_session.commit()
        svc = ConversationService(db_session)
        await svc.save_message(
            "kueche", "user", "intern", user_id=device.id, circle_tier=2,
        )
        await db_session.commit()

        conv = (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "kueche")
        )).scalar_one()
        assert await svc.reaches(conv.id, outsider.id) is False
        assert await svc.load_context(
            "kueche", user_id=outsider.id, enforce_ownership=True
        ) == []
        with pytest.raises(ConversationNotOwnedError):
            await svc.save_message(
                "kueche", "user", "rein da", user_id=outsider.id, enforce_ownership=True,
            )

    async def test_a_browser_chat_stays_personal(self, db_session, household):
        """Tier 0 is the default: Anna's own chat is hers, reach or no reach."""
        _, anna, bert = household
        svc = ConversationService(db_session)
        await svc.save_message("annas-chat", "user", "privat", user_id=anna.id)
        await db_session.commit()

        conv = (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "annas-chat")
        )).scalar_one()
        assert conv.circle_tier == 0
        assert await svc.reaches(conv.id, anna.id) is True
        assert await svc.reaches(conv.id, bert.id) is False


class TestTheAtom:
    async def test_an_owned_conversation_is_registered(self, db_session, household):
        """Never a bare INSERT (circles rule): the atom carries owner and tier,
        and that is what makes a per-person grant possible later."""
        device, _, _ = household
        svc = ConversationService(db_session)
        await svc.save_message(
            "kueche", "user", "x", user_id=device.id, circle_tier=2,
        )
        await db_session.commit()

        conv = (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "kueche")
        )).scalar_one()
        assert conv.atom_id is not None
        atom = (await db_session.execute(
            select(Atom).where(Atom.atom_id == conv.atom_id)
        )).scalar_one()
        assert atom.atom_type == ATOM_TYPE_CONVERSATION
        assert atom.owner_user_id == device.id
        assert atom.policy == {"tier": 2}
        assert atom.source_table == "conversations"
        assert atom.source_id == str(conv.id)

    async def test_an_ownerless_conversation_gets_none(self, db_session):
        """An atom's owner is NOT NULL. A row nobody owns waits for the backfill
        — and is refused under auth-on anyway."""
        svc = ConversationService(db_session)
        await svc.save_message("niemandes", "user", "x")
        await db_session.commit()

        conv = (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "niemandes")
        )).scalar_one()
        assert conv.atom_id is None

    async def test_an_explicit_grant_opens_one_conversation(self, db_session, household):
        """What the atom buys beyond the tier: sharing THIS thread with THIS
        person, without moving it to a wider tier."""
        _, anna, bert = household
        svc = ConversationService(db_session)
        await svc.save_message("annas-chat", "user", "privat", user_id=anna.id)
        await db_session.commit()
        conv = (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "annas-chat")
        )).scalar_one()
        assert await svc.reaches(conv.id, bert.id) is False

        await db_session.execute(
            text(
                "INSERT INTO atom_explicit_grants "
                "(atom_id, granted_to_user_id, permission_level, granted_by, granted_at) "
                "VALUES (:a, :to, 'read', :by, NOW())"
            ),
            {"a": conv.atom_id, "to": bert.id, "by": anna.id},
        )
        await db_session.commit()

        assert await svc.reaches(conv.id, bert.id) is True


class TestAlteringIsNotReaching:
    async def test_a_member_may_not_delete_the_shared_thread(self, db_session, household):
        """Reading a room history is for everyone it reaches; deleting one is
        not — `chat.own` would otherwise let any member wipe the kitchen."""
        device, anna, _ = household
        svc = ConversationService(db_session)
        await svc.save_message(
            "kueche", "user", "x", user_id=device.id, circle_tier=2,
        )
        await db_session.commit()
        conv = (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "kueche")
        )).scalar_one()

        assert ConversationService.may_alter(conv, anna.id) is False
        assert ConversationService.may_alter(
            conv, anna.id, caller_has_chat_all=True
        ) is True
        assert ConversationService.may_alter(conv, device.id) is True

    async def test_no_identity_keeps_the_legacy_behaviour(self, db_session, household):
        """`user_id=None` is the auth-off / device path: unchanged."""
        device, _, _ = household
        svc = ConversationService(db_session)
        await svc.save_message("kueche", "user", "x", user_id=device.id, circle_tier=2)
        await db_session.commit()
        conv = (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "kueche")
        )).scalar_one()
        assert ConversationService.may_alter(conv, None) is True


class TestMessageSearchFollowsReach:
    async def test_a_member_finds_what_was_said_in_the_room(self, db_session, household):
        """Decided deliberately (D-4a/§8.1): whoever may read the thread finds
        the line in it. A search that hid it would be a break in the logic —
        the same person sees it on opening the conversation."""
        device, anna, _ = household
        svc = ConversationService(db_session)
        await svc.save_message(
            "kueche", "user", "Schalte das Licht ein",
            user_id=device.id, circle_tier=2,
        )
        await db_session.commit()

        out = await svc.search_messages("Licht", user_id=anna.id)
        assert [r["session_id"] for r in out["results"]] == ["kueche"]

    async def test_it_does_not_reach_a_private_chat(self, db_session, household):
        _, anna, bert = household
        svc = ConversationService(db_session)
        await svc.save_message("annas-chat", "user", "Licht im Bad", user_id=anna.id)
        await db_session.commit()

        assert (await svc.search_messages("Licht", user_id=bert.id))["results"] == []
        assert [r["session_id"] for r in
                (await svc.search_messages("Licht", user_id=anna.id))["results"]] == [
            "annas-chat"
        ]


class TestTheListShowsSharedThreads:
    async def test_a_member_sees_the_room_history_in_their_list(
        self, db_session, household
    ):
        """Reach without listing would be useless: the thread exists but nobody
        finds it."""
        device, anna, _ = household
        svc = ConversationService(db_session)
        await svc.save_message(
            "kueche", "user", "hallo", user_id=device.id, circle_tier=2,
        )
        await svc.save_message("annas-chat", "user", "privat", user_id=anna.id)
        await db_session.commit()

        sessions = {c["session_id"] for c in await svc.list_all(user_id=anna.id)}
        assert sessions == {"kueche", "annas-chat"}


class TestMeetingsFollowTheSameRule:
    """`Meeting.circle_tier` has defaulted to 2 ("a meeting is a shared
    artifact") since meetings were introduced, while `GET /api/meetings` and
    every single-meeting route filtered on owner EQUALITY — the tier said
    shared and the query said private. Reading now follows reach; writing and
    deleting stay with the owner."""

    async def test_a_member_reaches_a_meeting(self, db_session, household):
        from models.database import Meeting
        from services.circle_sql import meetings_circles_filter

        device, anna, _ = household
        meeting = Meeting(
            owner_user_id=device.id, status="completed", circle_tier=2,
            consent_confirmed=True,
        )
        db_session.add(meeting)
        await db_session.commit()

        clause, params = meetings_circles_filter(anna.id, alias="meetings")
        reachable = (await db_session.execute(
            text(f"SELECT 1 FROM meetings WHERE id = :m AND {clause}"),
            {"m": meeting.id, **params},
        )).scalar()
        assert reachable is not None

    async def test_an_outsider_does_not(self, db_session, household):
        from models.database import Meeting
        from services.circle_sql import meetings_circles_filter

        device, _, _ = household
        outsider = await ensure_user(db_session, 9, username="fremd")
        meeting = Meeting(
            owner_user_id=device.id, status="completed", circle_tier=2,
            consent_confirmed=True,
        )
        db_session.add(meeting)
        await db_session.commit()

        clause, params = meetings_circles_filter(outsider.id, alias="meetings")
        reachable = (await db_session.execute(
            text(f"SELECT 1 FROM meetings WHERE id = :m AND {clause}"),
            {"m": meeting.id, **params},
        )).scalar()
        assert reachable is None

    async def test_writing_routes_stay_owner_bound(self):
        """Reach is not a licence to delete somebody else's recording: the
        write routes pass `for_write=True`, which keeps owner equality."""
        import inspect

        from api.routes import meetings as meetings_routes

        src = inspect.getsource(meetings_routes)
        assert src.count("for_write=True") == 6   # patch, delete, relabel, 3× minutes
        gate = inspect.getsource(meetings_routes._get_owned_meeting)
        assert "meeting.owner_user_id != user.id" in gate
        assert "meetings_circles_filter" in gate
