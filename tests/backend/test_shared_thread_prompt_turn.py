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
    """The first run searches and pins a variable; every run is recorded."""

    runs: list[dict] = []

    def __init__(self, *_a, **_kw):
        pass

    async def run(self, **kw):
        _FakeAgent.runs.append({
            "user_id": kw.get("user_id"),
            "history": "\n".join(m.get("content", "") for m in kw.get("conversation_history") or []),
            "context_vars_text": kw.get("context_vars_text", ""),
        })
        if len(_FakeAgent.runs) == 1:
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
