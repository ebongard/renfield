"""Chat document links: the CALL SITE hands the asker's permissions on.

`test_chat_document_links_pg.py` pins `turn_sources` with the permissions
passed in by hand. Here a real WebSocket turn runs through
`websocket_endpoint`: the handler loads the authenticated asker's role from
the database and must pass THOSE permissions to `_turn_sources`. Passing
`None` or `[]` there instead would strip the Paperless link from every
asker -- and passing anything that ignores the role would hand it to all.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.websocket.chat_handler as ch
import api.websocket.document_sources as ds
from services.agent_router import AgentRole
from services.agent_service import AgentStep
from services.input_guard import InjectionResult
from tests.backend.dbrows import ensure_user
from utils import hooks
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

PUBLIC = "https://paperless.example.org"
DOC_ID = 21
PAPERLESS_ID = 7001
LINK = f"{PUBLIC}/documents/{PAPERLESS_ID}/details"
WITH_PAPERLESS = 42
WITHOUT_PAPERLESS = 43


class _FakeAgent:
    """One `knowledge_search` that found document 21, then the answer."""

    def __init__(self, *_a, **_kw):
        pass

    async def run(self, **_kw):
        yield AgentStep(
            step_number=1, step_type="tool_result", tool="internal.knowledge_search",
            success=True, content="1 Treffer",
            data={"sources": [{"document_id": DOC_ID, "filename": "21.pdf",
                               "title": "Bescheid", "tier": 0}]},
        )
        yield AgentStep(step_number=2, step_type="final_answer", content="Im Bescheid steht …")


@pytest.fixture
async def askers(db_session):
    await ensure_user(
        db_session, WITH_PAPERLESS, username="mit", role_name="liest-paperless",
        permissions=["mcp.paperless.read"],
    )
    await ensure_user(
        db_session, WITHOUT_PAPERLESS, username="ohne", role_name="ohne-paperless",
        permissions=["kb.all"],
    )
    await db_session.commit()


@pytest.fixture
def asker():
    return {"id": WITH_PAPERLESS}


@pytest.fixture
def client(monkeypatch, askers, asker, own_session_to_test_db):
    async def _fake_auth(_ws, _token=None):
        return {"authenticated": True, "user_id": asker["id"], "auth_method": "jwt"}

    class _AllowAll:
        def check(self, _ip):
            return True, ""

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "chat_document_links_enabled", True, raising=False)
    monkeypatch.setattr(settings, "paperless_public_url", PUBLIC, raising=False)
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

    # The visibility lookup is not under test here (the pg tests own it): the
    # document is visible to both askers and filed in Paperless.
    async def _visible(doc_ids, user_id):
        return {DOC_ID: {"title": "Bescheid", "filename": "21.pdf", "tier": 0,
                         "paperless_document_id": PAPERLESS_ID,
                         "document_date": None, "page_count": None}}

    monkeypatch.setattr(ds, "_visible_meta", _visible)

    import services.action_executor as ae
    import services.agent_service as asvc
    import services.agent_tools as at

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
    app.state.ollama = SimpleNamespace(default_lang="de")
    app.state.agent_router = router
    app.include_router(ch.router)
    try:
        yield TestClient(app)
    finally:
        hooks._hooks.clear()
        hooks._hooks.update(saved)


def _done_sources(client) -> list[dict]:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "text", "content": "Was steht im Bescheid?"})
        while True:
            frame = ws.receive_json()
            if frame.get("type") == "done":
                return frame.get("sources", [])


def test_an_asker_with_the_paperless_permission_gets_the_link(client, asker):
    asker["id"] = WITH_PAPERLESS

    [source] = _done_sources(client)

    assert source["document_id"] == DOC_ID
    assert source["paperless_url"] == LINK


def test_an_asker_without_it_gets_the_source_and_no_link(client, asker):
    asker["id"] = WITHOUT_PAPERLESS

    [source] = _done_sources(client)

    assert source["document_id"] == DOC_ID
    assert "paperless_url" not in source
    assert str(PAPERLESS_ID) not in repr(source)
