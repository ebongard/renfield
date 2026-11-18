"""#1568 through the real WebSocket turn: a shared room thread, two members.

`test_shared_thread_prompt_state.py` pins the service rules. Here the chat
handler runs whole (`websocket_endpoint`): Anna's turn searches, its answer is
saved with her as the asker and her context variables; Bert then continues the
SAME thread from his own socket, and what reaches the agent -- the history and
the context-variable block -- must hold nothing of Anna's tool result. Anna's
next turn gets both back.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from starlette.testclient import TestClient

import api.websocket.chat_handler as ch
from models.database import Conversation, Message
from services.agent_router import AgentRole
from services.agent_service import AgentStep
from services.conversation_service import ConversationService
from services.input_guard import InjectionResult
from services.ollama_service import OllamaService
from tests.backend.dbrows import ensure_role, ensure_user
from utils import hooks
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

DEVICE, ANNA, BERT = 51, 52, 53
ROOM = "kueche-turn"
PREFIX = "[Aktionsergebnis"


class _FakeAgent:
    """The first run searches and pins a variable (unless `searches` is off);
    every run is recorded."""

    runs: list[dict] = []
    searches = True

    def __init__(self, *_a, **_kw):
        pass

    async def run(self, **kw):
        _FakeAgent.runs.append({
            "user_id": kw.get("user_id"),
            "history": "\n".join(m.get("content", "") for m in kw.get("conversation_history") or []),
            "context_vars_text": kw.get("context_vars_text", ""),
            "document_context": kw.get("document_context", ""),
        })
        if _FakeAgent.searches and len(_FakeAgent.runs) == 1:
            kw["context_vars_out"]["last_document_title"] = "Befund Anna"
            yield AgentStep(
                step_number=1, step_type="tool_result", tool="mcp.paperless.search_documents",
                success=True, content="1 Treffer",
                data={"results": [{"id": 4711, "title": "Befund Anna"}]},
            )
        yield AgentStep(step_number=2, step_type="final_answer", content="Erledigt.")


class _Ollama:
    """The two persistence methods of the real `OllamaService` -- the turn
    must load and save through the real path, which is what is under test."""

    default_lang = "de"
    load_conversation_context = OllamaService.load_conversation_context
    save_message = OllamaService.save_message


@pytest.fixture
async def room(db_session):
    await ensure_role(db_session)
    device = await ensure_user(db_session, DEVICE, username="haushalt-turn")
    device.is_device_account = True
    await ensure_user(db_session, ANNA, username="anna-turn")
    await ensure_user(db_session, BERT, username="bert-turn")
    await db_session.commit()
    for member in (ANNA, BERT):
        await db_session.execute(text(
            "INSERT INTO circle_memberships "
            "(circle_owner_id, member_user_id, dimension, value, granted_by, granted_at) "
            "VALUES (:o, :m, 'tier', '2', :o, NOW())"), {"o": DEVICE, "m": member})
    await ConversationService(db_session).save_message(
        ROOM, "user", "Guten Morgen", user_id=DEVICE, circle_tier=2)
    await db_session.commit()


@pytest.fixture
def asker():
    return {"id": ANNA}


@pytest.fixture
def client(monkeypatch, room, asker, own_session_to_test_db):
    async def _fake_auth(_ws, _token=None):
        return {"authenticated": True, "user_id": asker["id"], "auth_method": "jwt"}

    class _AllowAll:
        def check(self, _ip, **_kwargs):
            return True, ""

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(ch, "authenticate_websocket", _fake_auth)
    monkeypatch.setattr(ch, "get_rate_limiter", lambda: _AllowAll())
    monkeypatch.setattr(
        ch, "detect_injection",
        lambda _text: InjectionResult(score=0.0, blocked=False, matched_patterns=[]),
    )
    monkeypatch.setattr(ch, "_retrieve_memory_context", AsyncMock(return_value=""))
    monkeypatch.setattr(
        ch, "spawn_memory_extraction",
        lambda **_kw: SimpleNamespace(task=None, owns_post_message=False),
    )
    monkeypatch.setattr(ch, "spawn_post_message_hooks", lambda **_kw: None)

    import services.action_executor as ae
    import services.agent_service as asvc
    import services.agent_tools as at

    _FakeAgent.runs = []
    _FakeAgent.searches = True
    monkeypatch.setattr(asvc, "AgentService", _FakeAgent)
    monkeypatch.setattr(at.AgentToolRegistry, "create", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(ae, "ActionExecutor", lambda **_kw: MagicMock())

    role = AgentRole(name="documents", description={"de": "Dokumente"}, route_layer="semantic")
    router = SimpleNamespace(
        classify_with_context=AsyncMock(return_value=role),
        role_for_intent=lambda _i: None,
        roles={"documents": role},
    )

    saved = dict(hooks._hooks)
    hooks._hooks.clear()
    app = FastAPI()
    app.state.ollama = _Ollama()
    app.state.agent_router = router
    app.include_router(ch.router)
    try:
        yield TestClient(app)
    finally:
        hooks._hooks.clear()
        hooks._hooks.update(saved)


def _turn(client, content: str) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "text", "content": content, "session_id": ROOM})
        while True:
            frame = ws.receive_json()
            assert frame.get("type") != "session_replaced", "the member was swapped off the thread"
            if frame.get("type") == "done":
                return


async def test_the_next_member_gets_none_of_the_askers_results(client, asker, db_session):
    asker["id"] = ANNA
    _turn(client, "Such meinen Befund")
    asker["id"] = BERT
    _turn(client, "Und was gibt es Neues?")
    asker["id"] = ANNA
    _turn(client, "Öffne den zweiten")

    anna_1, bert, anna_2 = _FakeAgent.runs
    assert anna_1["user_id"] == ANNA and bert["user_id"] == BERT

    # Bert continues the thread: the text is shared, Anna's tool result is not.
    assert "Erledigt." in bert["history"]
    assert PREFIX not in bert["history"] and "4711" not in bert["history"]
    assert bert["context_vars_text"] == ""

    # Anna's own next turn has both.
    assert PREFIX in anna_2["history"] and "4711" in anna_2["history"]
    assert "last_document_title: Befund Anna" in anna_2["context_vars_text"]

    # The asker is recorded server-side on every answer.
    conv = (await db_session.execute(
        select(Conversation).where(Conversation.session_id == ROOM))).scalar_one()
    metas = (await db_session.execute(
        select(Message.message_metadata)
        .where(Message.conversation_id == conv.id, Message.role == "assistant")
        .order_by(Message.id))).scalars().all()
    assert [m.get("asker_user_id") for m in metas] == [ANNA, BERT, ANNA]
    assert "action_summary" in metas[0] and "action_summary" not in metas[1]


async def _assistant_metas(db_session, session_id: str = ROOM) -> list[dict]:
    db_session.expire_all()
    conv = (await db_session.execute(
        select(Conversation).where(Conversation.session_id == session_id))).scalar_one()
    return list((await db_session.execute(
        select(Message.message_metadata)
        .where(Message.conversation_id == conv.id, Message.role == "assistant")
        .order_by(Message.id))).scalars().all())


async def _stored_vars(db_session, session_id: str = ROOM):
    db_session.expire_all()
    return (await db_session.execute(
        select(Conversation.context_vars).where(Conversation.session_id == session_id)
    )).scalar_one()


# ============================================================================
# A voice-promoted identity is not an asker
# ============================================================================

async def test_a_voice_promoted_turn_records_no_asker_and_keys_no_vars(
    client, asker, db_session, monkeypatch,
):
    """Under auth, a socket without a JWT user whose voice is recognised runs
    AS that person (circle reach), but the asker is the SOCKET identity: the
    history and the variables are read under the JWT, and a promoted id must
    not key state another socket could later read as that person's."""
    asker["id"] = None
    monkeypatch.setattr(ch, "_resolve_wire_speaker", AsyncMock(return_value={
        "speaker_id": 9, "speaker_name": "anna", "speaker_confidence": 0.99}))
    monkeypatch.setattr(ch, "_lookup_user_id_for_speaker", AsyncMock(return_value=ANNA))

    _turn(client, "Such meinen Befund")

    [run] = _FakeAgent.runs
    assert run["user_id"] == ANNA, "precondition: the voice promotion took place"
    metas = await _assistant_metas(db_session)
    assert len(metas) == 1 and "asker_user_id" not in metas[0]
    assert "_per_writer" not in (await _stored_vars(db_session) or {})


# ============================================================================
# A device account is nobody's asker (#1578 review)
# ============================================================================

async def test_a_device_account_socket_keys_nothing_for_the_next_person(
    client, asker, db_session,
):
    """A room panel logged in with the device account is used by Anna, then by
    Bert -- the same JWT both times. Keyed on the device account, Anna's tool
    result would reach Bert. The satellite path refused that already; the
    browser path now does too."""
    asker["id"] = DEVICE
    _turn(client, "Such meinen Befund")
    _turn(client, "Und was gibt es Neues?")

    first, second = _FakeAgent.runs
    assert first["user_id"] == DEVICE, "precondition: the panel's turn ran"
    assert PREFIX not in second["history"] and "4711" not in second["history"]
    assert second["context_vars_text"] == ""
    metas = await _assistant_metas(db_session)
    assert all("asker_user_id" not in m for m in metas)
    assert "_per_writer" not in (await _stored_vars(db_session) or {})


# ============================================================================
# The REST fallback: same gate
# ============================================================================

async def test_rest_send_gives_the_next_member_none_of_the_askers_vars(
    async_client, room, db_session, monkeypatch,
):
    import services.action_executor as ae
    import services.agent_service as asvc
    import services.agent_tools as at
    from main import app
    from models.database import User
    from services.auth_service import get_current_user
    from services.complexity_detector import ComplexityDetector

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(ComplexityDetector, "needs_agent_with_feedback", AsyncMock(return_value=True))
    _FakeAgent.runs, _FakeAgent.searches = [], False
    monkeypatch.setattr(asvc, "AgentService", _FakeAgent)
    monkeypatch.setattr(at.AgentToolRegistry, "create", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(ae, "ActionExecutor", lambda **_kw: MagicMock())
    monkeypatch.setattr(app.state, "ollama", MagicMock(), raising=False)

    await ConversationService(db_session).save_turn_context_vars(
        ROOM, {"last_document_title": "Befund Anna"}, user_id=ANNA, enforce_ownership=True)
    bert = (await db_session.execute(select(User).where(User.id == BERT))).scalar_one()
    app.dependency_overrides[get_current_user] = lambda: bert

    r = await async_client.post("/api/chat/send", json={"message": "Was gibt es Neues?", "session_id": ROOM})
    assert r.status_code == 200, r.text
    assert r.json()["session_id"] == ROOM, "precondition: Bert continues the shared thread"

    [run] = _FakeAgent.runs
    assert run["user_id"] == BERT
    assert run["context_vars_text"] == ""
    assert (await _assistant_metas(db_session))[-1].get("asker_user_id") == BERT


# ============================================================================
# A pending Paperless confirm is the uploader's
# ============================================================================

class _RecordingExecutor:
    calls: list[dict] = []

    def __init__(self, **_kw):
        pass

    async def execute(self, intent, **kw):
        _RecordingExecutor.calls.append({**intent, "user_id": kw.get("user_id")})
        return {"success": True, "message": "Abgelegt.", "action_taken": True}


async def _pending_confirm(db_session, owner: int) -> str:
    import uuid

    from models.database import ChatUpload, PaperlessPendingConfirm

    upload = ChatUpload(
        session_id=ROOM, filename="befund.pdf", status="completed", uploaded_by_user_id=owner)
    db_session.add(upload)
    await db_session.flush()
    token = str(uuid.uuid4())
    db_session.add(PaperlessPendingConfirm(
        confirm_token=token, attachment_id=upload.id, session_id=ROOM, user_id=owner,
        llm_output={}, post_fuzzy_output={}, proposals=[],
    ))
    await db_session.commit()
    return token


async def test_another_members_yes_neither_commits_nor_sees_the_confirm(
    client, asker, db_session, monkeypatch,
):
    import services.action_executor as ae

    _FakeAgent.searches = False
    _RecordingExecutor.calls = []
    monkeypatch.setattr(ae, "ActionExecutor", _RecordingExecutor)
    token = await _pending_confirm(db_session, ANNA)

    asker["id"] = BERT
    _turn(client, "ja")
    assert _RecordingExecutor.calls == [], "Bert's yes must not commit Anna's document"
    [bert] = _FakeAgent.runs
    assert "PENDING_PAPERLESS_CONFIRM" not in bert["document_context"]
    assert token not in bert["document_context"]

    asker["id"] = ANNA
    _turn(client, "ja")
    [commit] = _RecordingExecutor.calls
    assert commit["intent"] == "internal.paperless_commit_upload"
    assert commit["parameters"]["confirm_token"] == token and commit["user_id"] == ANNA


async def test_the_commit_tool_refuses_another_members_token(
    room, db_session, own_session_to_test_db, monkeypatch,
):
    from models.database import PaperlessPendingConfirm
    from services.paperless_commit_tool import paperless_commit_upload

    monkeypatch.setattr(settings, "auth_enabled", True)
    token = await _pending_confirm(db_session, ANNA)
    mcp = MagicMock()
    mcp.execute_tool = AsyncMock()

    for caller in (BERT, None):
        r = await paperless_commit_upload(
            {"confirm_token": token, "user_response_text": "nein"},
            mcp_manager=mcp, session_id=ROOM, user_id=caller,
        )
        assert r["success"] is False and "abgelaufen oder unbekannt" in r["message"]
    db_session.expire_all()
    assert await db_session.get(PaperlessPendingConfirm, token) is not None, "nothing was aborted"

    # Anna's own answer still reaches it ("nein" aborts without Paperless).
    r = await paperless_commit_upload(
        {"confirm_token": token, "user_response_text": "nein"},
        mcp_manager=mcp, session_id=ROOM, user_id=ANNA,
    )
    assert r["success"] is True
    db_session.expire_all()
    assert await db_session.get(PaperlessPendingConfirm, token) is None
