"""A shared thread hands no member's tool results to the next member's prompt (#1568).

A room history belongs to the device account at tier 2, and every member whose
tier reaches it continues the ONE thread; a conversation above tier 0 is
reached by others too, and a handoff copies messages into a room thread. Two
carriers re-enter the prompt from such a thread:

- `message_metadata.action_summary` (the asker's tool result: Paperless ids and
  titles, retrieved text), re-injected by `ConversationService.load_context`;
- `conversations.context_vars`, rendered into the agent prompt.

Owner decision: every assistant message names its asker (`asker_user_id`),
every context variable its writer; under auth a caller gets only their own.
Rows without one (legacy) reach nobody, the owner of a private conversation
included; no caller identity gets nothing; auth-off is unchanged.

Everything here runs on real Postgres: reach is the four-branch circle SQL.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Conversation, Message
from services.conversation_service import ConversationService
from tests.backend.dbrows import ensure_role, ensure_speaker, ensure_user
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "src" / "backend"

DEVICE, ANNA, BERT = 1, 2, 3
#: The stored key -- pinned by name: it is persisted, so renaming it would
#: turn every recorded row into a legacy one.
ASKER = "asker_user_id"
PREFIX = "[Aktionsergebnis"
A_SUMMARY = "mcp.paperless.search_documents → 1 Ergebnisse:\n  - id=4711, title=Befund Anna"


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
async def household(db_session: AsyncSession, monkeypatch):
    """Device account plus Anna and Bert reaching its tier 2. Auth ON."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    await ensure_role(db_session)
    device = await ensure_user(db_session, DEVICE, username="haushalt")
    device.is_device_account = True
    await ensure_user(db_session, ANNA, username="anna")
    await ensure_user(db_session, BERT, username="bert")
    await db_session.commit()
    await _member_of(db_session, DEVICE, ANNA, 2)
    await _member_of(db_session, DEVICE, BERT, 2)


async def _thread(db, session_id: str, owner: int, tier: int) -> None:
    svc = ConversationService(db)
    await svc.save_message(session_id, "user", "Hallo", user_id=owner, circle_tier=tier)
    await db.commit()


async def _turn(db, session_id: str, asker: int | None, summary: str, *, legacy=False) -> None:
    """One answered turn by `asker`, as the browser handler persists it."""
    svc = ConversationService(db)
    meta = {"intent": "agent", "action_success": True, "action_summary": summary}
    if not legacy:
        meta[ASKER] = asker
    await svc.save_message(
        session_id, "user", "Such meinen Befund", user_id=asker,
        enforce_ownership=settings.auth_enabled,
    )
    await svc.save_message(
        session_id, "assistant", "Gefunden.", metadata=meta, user_id=asker,
        enforce_ownership=settings.auth_enabled,
    )
    await db.commit()


async def _prompt(db, session_id: str, caller: int | None, *, enforce=True) -> str:
    ctx = await ConversationService(db).load_context(
        session_id, user_id=caller, enforce_ownership=enforce and settings.auth_enabled,
    )
    for m in ctx:
        assert "action_summary" not in (m.get("metadata") or {}) or PREFIX in m["content"], (
            "a refused summary must not ride along in the metadata")
    return "\n".join(m["content"] for m in ctx)


async def _vars(db, session_id: str, caller: int | None) -> dict:
    return await ConversationService(db).load_context_vars(
        session_id, user_id=caller, enforce_ownership=settings.auth_enabled,
    )


async def _write_vars(db, session_id: str, writer: int | None, values: dict) -> dict:
    return await ConversationService(db).save_turn_context_vars(
        session_id, values, user_id=writer, enforce_ownership=settings.auth_enabled,
    )


async def _stored(db, session_id: str):
    db.expire_all()
    return (await db.execute(
        select(Conversation.context_vars).where(Conversation.session_id == session_id)
    )).scalar_one()


# ============================================================================
# The shared thread: the asker gets their results, the next member does not
# ============================================================================

class TestSharedThread:
    @pytest.mark.parametrize("shape", ["room_tier2_device_owner", "member_owned_tier1"])
    async def test_the_next_member_gets_neither_the_asker_gets_both(
        self, db_session, household, shape,
    ):
        if shape == "room_tier2_device_owner":
            await _thread(db_session, "kueche", DEVICE, 2)
        else:
            # A conversation of Anna's own, shared at tier 1 with Bert.
            await _member_of(db_session, ANNA, BERT, 1)
            await _thread(db_session, "kueche", ANNA, 1)

        await _turn(db_session, "kueche", ANNA, A_SUMMARY)
        await _write_vars(db_session, "kueche", ANNA, {"last_document_title": "Befund Anna"})

        bert_prompt = await _prompt(db_session, "kueche", BERT)
        assert "Gefunden." in bert_prompt, "Bert reaches the thread -- the text is shared"
        assert PREFIX not in bert_prompt and "Befund Anna" not in bert_prompt
        assert await _vars(db_session, "kueche", BERT) == {}

        anna_prompt = await _prompt(db_session, "kueche", ANNA)
        assert PREFIX in anna_prompt and "id=4711" in anna_prompt
        assert await _vars(db_session, "kueche", ANNA) == {"last_document_title": "Befund Anna"}

    async def test_one_members_turn_neither_erases_nor_overwrites_anothers(
        self, db_session, household,
    ):
        """Bert's turn writes the SAME key and clears another -- Anna's next
        load still sees exactly what she wrote."""
        await _thread(db_session, "kueche", DEVICE, 2)
        await _write_vars(db_session, "kueche", ANNA, {
            "last_document_title": "Befund Anna", "last_document_id": 4711})
        await _write_vars(db_session, "kueche", BERT, {
            "last_document_title": "Rechnung Bert", "last_document_id": None})
        await ConversationService(db_session).save_context_vars(
            "kueche", {"_active_domain": "media"}, user_id=BERT, enforce_ownership=True)

        assert await _vars(db_session, "kueche", ANNA) == {
            "last_document_title": "Befund Anna", "last_document_id": 4711}
        assert await _vars(db_session, "kueche", BERT) == {
            "last_document_title": "Rechnung Bert", "_active_domain": "media"}
        # The routing state is per writer as well: Bert's domain does not
        # steer Anna's next turn.
        assert "_active_domain" not in await _vars(db_session, "kueche", ANNA)

    async def test_the_device_owner_gets_no_members_results(self, db_session, household):
        await _thread(db_session, "kueche", DEVICE, 2)
        await _turn(db_session, "kueche", ANNA, A_SUMMARY)
        await _write_vars(db_session, "kueche", ANNA, {"last_document_title": "Befund Anna"})
        assert PREFIX not in await _prompt(db_session, "kueche", DEVICE)
        assert await _vars(db_session, "kueche", DEVICE) == {}


# ============================================================================
# Legacy rows: no asker recorded
# ============================================================================

class TestLegacyRows:
    """Owner decision: a tool result that names no writer reaches NO prompt
    under auth -- private conversation and owner included. Nobody can say
    whose it is."""

    async def _legacy(self, db, session_id: str, owner: int, tier: int) -> None:
        await _thread(db, session_id, owner, tier)
        await _turn(db, session_id, owner, A_SUMMARY, legacy=True)
        conv = (await db.execute(
            select(Conversation).where(Conversation.session_id == session_id))).scalar_one()
        conv.context_vars = {"last_document_title": "Befund alt", "_active_domain": "documents"}
        await db.commit()

    @pytest.mark.parametrize(("session_id", "owner", "tier"), [
        ("privat", ANNA, 0), ("geteilt", ANNA, 1), ("kueche", DEVICE, 2),
    ])
    async def test_they_reach_nobody(self, db_session, household, session_id, owner, tier):
        if tier == 1:
            await _member_of(db_session, ANNA, BERT, 1)
        await self._legacy(db_session, session_id, owner, tier)
        for caller in (DEVICE, ANNA, BERT, None):
            prompt = await _prompt(db_session, session_id, caller)
            assert PREFIX not in prompt
            assert await _vars(db_session, session_id, caller) == {}
        # The thread itself stays readable where it is reached.
        assert "Gefunden." in await _prompt(db_session, session_id, owner)

    @pytest.mark.parametrize(("session_id", "tier"), [("privat", 0), ("geteilt", 1)])
    async def test_a_write_neither_folds_nor_erases_the_legacy_layer(
        self, db_session, household, session_id, tier,
    ):
        """The writer gets only what they write -- the owner of a private
        conversation too: folding the legacy layer into their bucket would
        hand it to them. It stays stored at the top level, untouched."""
        if tier == 1:
            await _member_of(db_session, ANNA, BERT, 1)
        await self._legacy(db_session, session_id, ANNA, tier)
        await _write_vars(db_session, session_id, ANNA, {"x": 1, "last_document_title": None})

        assert await _vars(db_session, session_id, ANNA) == {"x": 1}
        assert await _vars(db_session, session_id, BERT) == {}
        assert await _stored(db_session, session_id) == {
            "last_document_title": "Befund alt", "_active_domain": "documents",
            "_per_writer": {str(ANNA): {"x": 1}},
        }


# ============================================================================
# No caller identity; auth-off
# ============================================================================

class TestNoIdentityAndAuthOff:
    async def test_satellite_without_a_speaker_gets_nothing(self, db_session, household):
        """The satellite loads without `enforce_ownership` and, with no
        recognised speaker, without identity: no summary at all, own or legacy."""
        await _thread(db_session, "kueche", DEVICE, 2)
        await _turn(db_session, "kueche", ANNA, A_SUMMARY)
        await _turn(db_session, "kueche", ANNA, "alt → x", legacy=True)
        prompt = await _prompt(db_session, "kueche", None, enforce=False)
        assert "Gefunden." in prompt and PREFIX not in prompt
        assert await ConversationService(db_session).load_context_vars("kueche") == {}

    async def test_satellite_with_the_asker_speaking_gets_them(self, db_session, household):
        await _thread(db_session, "kueche", DEVICE, 2)
        await _turn(db_session, "kueche", ANNA, A_SUMMARY)
        assert PREFIX in await _prompt(db_session, "kueche", ANNA, enforce=False)
        assert PREFIX not in await _prompt(db_session, "kueche", BERT, enforce=False)

    async def test_no_writer_under_auth_writes_nothing(self, db_session, household):
        await _thread(db_session, "kueche", DEVICE, 2)
        assert await ConversationService(db_session).save_context_vars(
            "kueche", {"last_document_id": 1}) is False
        assert await _stored(db_session, "kueche") is None

    async def test_auth_off_is_unchanged(self, db_session, household, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", False)
        await _thread(db_session, "raum", None, 0)
        await _turn(db_session, "raum", None, A_SUMMARY, legacy=True)
        await _turn(db_session, "raum", ANNA, "zweite → y")  # an asker recorded, ignored
        await _write_vars(db_session, "raum", None, {"last_document_title": "Befund"})
        await _write_vars(db_session, "raum", BERT, {"last_document_id": 4711})

        for caller in (None, ANNA, BERT):
            prompt = await _prompt(db_session, "raum", caller, enforce=False)
            assert prompt.count(PREFIX) == 2
            assert await _vars(db_session, "raum", caller) == {
                "last_document_title": "Befund", "last_document_id": 4711}
        # Storage keeps the flat shape it always had.
        assert await _stored(db_session, "raum") == {
            "last_document_title": "Befund", "last_document_id": 4711}


# ============================================================================
# The private chat stays the same in effect
# ============================================================================

class TestPrivateChatUnchanged:
    async def _run(self, db, session_id: str, user: int | None) -> tuple[str, list[dict]]:
        """The same private conversation through several new turns."""
        svc = ConversationService(db)
        await _thread(db, session_id, user, 0)
        await _write_vars(db, session_id, user, {
            "last_document_title": "Alt", "last_document_correspondent": "Telekom"})

        views = [await _vars(db, session_id, user)]
        await _turn(db, session_id, user, A_SUMMARY)
        # the second hit has no correspondent: the old one must go (delete marker)
        await _write_vars(db, session_id, user, {
            "last_document_title": "Neu", "last_document_correspondent": None})
        views.append(await _vars(db, session_id, user))
        await svc.save_context_vars(
            session_id, {"_active_domain": "documents"}, user_id=user,
            enforce_ownership=settings.auth_enabled)
        await _write_vars(db, session_id, user, {"last_document_id": 9})
        views.append(await _vars(db, session_id, user))
        return await _prompt(db, session_id, user), views

    async def test_auth_on_private_equals_auth_off(self, db_session, household, monkeypatch):
        on_prompt, on_views = await self._run(db_session, "privat-an", ANNA)

        monkeypatch.setattr(settings, "auth_enabled", False)
        off_prompt, off_views = await self._run(db_session, "privat-aus", ANNA)

        assert on_prompt == off_prompt
        assert on_prompt.count(PREFIX) == 1
        assert on_views == off_views
        assert on_views[0] == {"last_document_title": "Alt", "last_document_correspondent": "Telekom"}
        assert on_views[1] == {"last_document_title": "Neu"}
        assert list(on_views[2]) == list(off_views[2]), "recency order is kept too"


# ============================================================================
# Handoff carries the asker along
# ============================================================================

class TestHandoff:
    async def _source(self, db) -> int:
        from services import conversation_handoff

        conversation_handoff._last_handoff.clear()
        speaker_id = (await ensure_speaker(db, 1, "anna")).id
        await _thread(db, "satellite-kueche", DEVICE, 2)
        await _turn(db, "satellite-kueche", ANNA, A_SUMMARY)
        await _write_vars(db, "satellite-kueche", ANNA, {"last_document_title": "Befund Anna"})
        db.expire_all()
        source = (await db.execute(select(Conversation).where(
            Conversation.session_id == "satellite-kueche"))).scalar_one()
        source.speaker_id = speaker_id
        await db.commit()
        return speaker_id

    async def test_the_asker_travels_into_the_target_room(self, db_session, household):
        from services.conversation_handoff import try_handoff_context

        speaker_id = await self._source(db_session)
        assert await try_handoff_context(speaker_id, "satellite-bad", db_session) is True

        target = (await db_session.execute(select(Conversation).where(
            Conversation.session_id == "satellite-bad"))).scalar_one()
        copied = (await db_session.execute(select(Message.message_metadata).where(
            Message.conversation_id == target.id, Message.role == "assistant"))).scalars().all()
        assert [m.get(ASKER) for m in copied if m and m.get("action_summary")] == [ANNA]

        assert PREFIX in await _prompt(db_session, "satellite-bad", ANNA)
        assert "Gefunden." in await _prompt(db_session, "satellite-bad", BERT)
        assert PREFIX not in await _prompt(db_session, "satellite-bad", BERT)
        assert await _vars(db_session, "satellite-bad", ANNA) == {"last_document_title": "Befund Anna"}
        assert await _vars(db_session, "satellite-bad", BERT) == {}

    async def test_an_existing_target_keeps_the_other_members_variables(
        self, db_session, household,
    ):
        from services.conversation_handoff import try_handoff_context

        # The target room already holds Bert's own variables.
        await _thread(db_session, "satellite-bad", DEVICE, 2)
        await _write_vars(db_session, "satellite-bad", BERT, {"last_media_name": "Bert"})
        await db_session.execute(text(
            "UPDATE conversations SET updated_at = updated_at - interval '1 minute' "
            "WHERE session_id = 'satellite-bad'"))
        await db_session.commit()
        speaker_id = await self._source(db_session)

        assert await try_handoff_context(speaker_id, "satellite-bad", db_session) is True

        assert await _vars(db_session, "satellite-bad", ANNA) == {"last_document_title": "Befund Anna"}
        assert await _vars(db_session, "satellite-bad", BERT) == {"last_media_name": "Bert"}


# ============================================================================
# The satellite's identity and its room list
# ============================================================================

class TestSatelliteIdentity:
    async def test_the_prompt_identity_is_the_person_never_the_device(
        self, db_session, household, own_session_to_test_db,
    ):
        from ha_glue.api.websocket.satellite_handler import speaker_prompt_identity
        from models.database import User

        anna_spk = await ensure_speaker(db_session, 1, "anna")
        dev_spk = await ensure_speaker(db_session, 2, "haushalt")
        await ensure_speaker(db_session, 3, "gast")
        for uid, spk in ((ANNA, anna_spk), (DEVICE, dev_spk)):
            (await db_session.execute(select(User).where(User.id == uid))).scalar_one().speaker_id = spk.id
        await db_session.commit()

        assert await speaker_prompt_identity(anna_spk.id, 0.9) == ANNA
        assert await speaker_prompt_identity(dev_spk.id, 0.9) is None
        assert await speaker_prompt_identity(3, 0.9) is None
        assert await speaker_prompt_identity(None, 0.9) is None

    async def test_below_the_identity_floor_the_speaker_is_unknown(
        self, db_session, household, own_session_to_test_db, monkeypatch,
    ):
        """The browser's voice-promotion floor: a weaker match is no identity
        claim, so it gets no asker and no stored results of its own."""
        from ha_glue.api.websocket.satellite_handler import speaker_prompt_identity
        from models.database import User

        monkeypatch.setattr(settings, "voice_auth_min_confidence", 0.7)
        spk = await ensure_speaker(db_session, 1, "anna")
        (await db_session.execute(select(User).where(User.id == ANNA))).scalar_one().speaker_id = spk.id
        await db_session.commit()

        assert await speaker_prompt_identity(spk.id, 0.69) is None
        assert await speaker_prompt_identity(spk.id, 0.7) == ANNA


@pytest.mark.unit
class TestRoomHistoryRefresh:
    """The satellite's in-memory list is the ROOM's; it must not carry one
    speaker's results to the next."""

    def _ollama(self, by_caller: dict):
        calls = []

        async def load(session_id, db, max_messages=20, user_id=None, enforce_ownership=False):
            calls.append(user_id)
            if by_caller.get(user_id) == "boom":
                raise RuntimeError("db down")
            return list(by_caller.get(user_id, []))

        return SimpleNamespace(load_conversation_context=load), calls

    async def _refresh(self, ollama, history, loaded, loaded_for, caller):
        from ha_glue.api.websocket.satellite_handler import refresh_room_history

        return await refresh_room_history(
            ollama, "satellite-x", history, loaded=loaded, loaded_for=loaded_for, caller=caller)

    async def test_a_new_speaker_reloads_and_the_old_list_goes(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", True)
        ollama, calls = self._ollama({ANNA: [{"content": "A-Ergebnis"}], BERT: [{"content": "B"}]})
        history: list = []
        state = await self._refresh(ollama, history, False, None, ANNA)
        assert state == (True, ANNA) and history == [{"content": "A-Ergebnis"}]
        assert await self._refresh(ollama, history, *state, ANNA) == (True, ANNA)
        assert calls == [ANNA], "the same speaker keeps the once-per-day load"

        state = await self._refresh(ollama, history, True, ANNA, BERT)
        assert state == (True, BERT) and history == [{"content": "B"}]
        state = await self._refresh(ollama, history, *state, None)
        assert state == (True, None) and history == []

    async def test_a_failed_reload_leaves_nothing_behind(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", True)
        ollama, _ = self._ollama({BERT: "boom"})
        history = [{"content": "A-Ergebnis"}]
        assert await self._refresh(ollama, history, True, ANNA, BERT) == (True, ANNA)
        assert history == []

    async def test_auth_off_loads_once(self, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", False)
        ollama, calls = self._ollama({ANNA: [{"content": "A"}]})
        history: list = []
        state = await self._refresh(ollama, history, False, None, ANNA)
        assert await self._refresh(ollama, history, *state, BERT) == state
        assert calls == [ANNA] and history == [{"content": "A"}]


# ============================================================================
# Pure storage helpers
# ============================================================================

@pytest.mark.unit
class TestWriterBuckets:
    def test_merge_touches_only_the_writers_bucket(self):
        from services.context_extractor import WRITERS_KEY, merge_writer_context_vars

        stored = {"legacy": 1, WRITERS_KEY: {"3": {"k": "bert"}}}
        out = merge_writer_context_vars(stored, 2, {"k": "anna"})
        assert out == {"legacy": 1, WRITERS_KEY: {"3": {"k": "bert"}, "2": {"k": "anna"}}}
        out = merge_writer_context_vars(out, 2, {"k": None})
        assert out == {"legacy": 1, WRITERS_KEY: {"3": {"k": "bert"}}}

    def test_a_delete_marker_never_reaches_the_legacy_layer(self):
        from services.context_extractor import WRITERS_KEY, merge_writer_context_vars

        out = merge_writer_context_vars({"a": 1, "b": 2}, 2, {"b": None, "c": 3})
        assert out == {"a": 1, "b": 2, WRITERS_KEY: {"2": {"c": 3}}}

    def test_handoff_without_buckets_is_the_plain_copy(self):
        from services.context_extractor import handoff_context_vars

        assert handoff_context_vars({"a": 1}, {"b": 2}) == {"a": 1}

    def test_a_tool_or_plugin_key_cannot_reach_the_buckets(self):
        from services.context_extractor import WRITERS_KEY, bound_context_vars

        assert bound_context_vars({WRITERS_KEY: {"2": {"x": 1}}}) == {}


# ============================================================================
# Never served; every load passes its caller
# ============================================================================

@pytest.mark.unit
async def test_the_asker_is_never_served_to_a_client():
    import api.websocket.document_sources as ds
    from services.conversation_service import ASKER_METADATA_KEY

    assert ASKER_METADATA_KEY == ASKER
    assert ASKER not in ds.CLIENT_METADATA_KEYS
    [served] = await ds.metadata_for_reader(
        [{ASKER: ANNA, "action_summary": A_SUMMARY, "agent_role": "documents"}], None)
    assert served == {"agent_role": "documents"}


#: The loads that feed a prompt, and the writes that key a value on its
#: writer. Every call site in the backend passes the caller as `user_id=`.
_IDENTITY_CALLS = {
    "load_context", "load_conversation_context", "load_context_vars",
    "save_context_vars", "save_turn_context_vars",
}


def _calls_missing_the_caller() -> list[str]:
    missing = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        rel = path.relative_to(BACKEND_ROOT)
        if rel.parts[0] in {"satellite", "alembic"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in _IDENTITY_CALLS:
                continue
            if not any(kw.arg == "user_id" for kw in node.keywords):
                missing.append(f"{rel}:{node.lineno} {node.func.attr}")
    return missing


@pytest.mark.unit
def test_every_call_site_passes_the_caller_identity():
    """The class, not the instance: a new prompt load without `user_id=` would
    read as "no identity" -- under auth nothing, but under the satellite's
    old call it was simply never asked."""
    assert _calls_missing_the_caller() == []


@pytest.mark.unit
def test_the_structural_check_sees_a_missing_caller(tmp_path, monkeypatch):
    """Negative control: the walker reports a call without `user_id=`."""
    import tests.backend.test_shared_thread_prompt_state as me

    (tmp_path / "x.py").write_text(
        "async def f(svc):\n"
        "    '''svc.load_context(sid) in prose does not count'''\n"
        "    await svc.load_context(sid)\n"
        "    await svc.load_context_vars(sid, user_id=u)\n"
    )
    monkeypatch.setattr(me, "BACKEND_ROOT", tmp_path)
    assert me._calls_missing_the_caller() == ["x.py:3 load_context"]


# ============================================================================
# Every recorded asker went through a resolver that refuses device accounts
# ============================================================================

#: The functions that turn a server-side identity into an asker. Both return
#: None for a device account (a room panel / a satellite's account is every
#: person in front of it, so a result keyed on it reaches the next one).
_ASKER_RESOLVERS = {"prompt_asker", "speaker_prompt_identity"}


def _calls_resolver(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", None)) in _ASKER_RESOLVERS
        for n in ast.walk(node)
    )


def _askers_not_resolved() -> list[str]:
    """`record_asker(meta, X)` where X is neither a resolver call nor a name
    whose every assignment in the enclosing function is None or a resolver call."""
    bad = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        rel = path.relative_to(BACKEND_ROOT)
        if rel.parts[0] in {"satellite", "alembic"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigned: dict[str, list[ast.AST]] = {}
            for n in ast.walk(func):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            assigned.setdefault(t.id, []).append(n.value)
            for n in ast.walk(func):
                if not (isinstance(n, ast.Call)
                        and getattr(n.func, "id", getattr(n.func, "attr", None)) == "record_asker"):
                    continue
                if len(n.args) < 2:
                    bad.append(f"{rel}:{n.lineno}")
                    continue
                arg = n.args[1]
                if _calls_resolver(arg):
                    continue
                values = assigned.get(arg.id, []) if isinstance(arg, ast.Name) else []
                if values and all(
                    (isinstance(v, ast.Constant) and v.value is None) or _calls_resolver(v)
                    for v in values
                ):
                    continue
                bad.append(f"{rel}:{n.lineno}")
    return sorted(set(bad))


@pytest.mark.unit
def test_every_recorded_asker_went_through_a_resolver():
    """The class, not the instance (#1578 review): the browser and the REST
    path recorded the JWT user as the asker, a device account included, while
    the satellite path already refused it. Every `record_asker` now takes its
    asker from `prompt_asker` / `speaker_prompt_identity`."""
    assert _askers_not_resolved() == []


@pytest.mark.unit
def test_the_resolver_check_sees_a_raw_identity(tmp_path, monkeypatch):
    """Negative control: a raw user id, and a name with one raw assignment."""
    import tests.backend.test_shared_thread_prompt_state as me

    (tmp_path / "x.py").write_text(
        "async def f(db, user_id, meta):\n"
        "    record_asker(meta, user_id)\n"
        "    a = None\n"
        "    a = user_id\n"
        "    record_asker(meta, a)\n"
        "    b = None\n"
        "    b = await prompt_asker(db, user_id)\n"
        "    record_asker(meta, b)\n"
        "    record_asker(meta, await prompt_asker(db, user_id))\n"
    )
    monkeypatch.setattr(me, "BACKEND_ROOT", tmp_path)
    assert me._askers_not_resolved() == ["x.py:2", "x.py:5"]


@pytest.mark.database
async def test_prompt_asker_refuses_a_device_account(db_session):
    from services.conversation_service import prompt_asker
    from tests.backend.dbrows import ensure_role, ensure_user

    await ensure_role(db_session)
    panel = await ensure_user(db_session, 61, username="panel-asker")
    panel.is_device_account = True
    await ensure_user(db_session, 62, username="person-asker")
    await db_session.commit()

    assert await prompt_asker(db_session, 61) is None
    assert await prompt_asker(db_session, 62) == 62
    assert await prompt_asker(db_session, None) is None
    assert await prompt_asker(db_session, 9999) is None  # unknown: nobody
