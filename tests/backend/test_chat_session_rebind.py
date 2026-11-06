"""A socket switching conversation starts from a clean history (#1485 review).

The in-memory history, its load flag and its watermark describe ONE
conversation. The `register` frame -- sent by the browser on EVERY session
change of a live socket -- used to move `db_session_id` and leave the rest: the
next message then saw the same id, skipped the reload, and sent the previous
conversation's turns in the new one's prompt. Concretely: the tab shows A, the
user opens a deleted conversation B from a stale sidebar, B is swapped for a
fresh F, and F's first turn continued A.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.websocket.chat_handler as ch
from api.websocket.shared import ConversationSessionState, history_needs_reload

pytestmark = [pytest.mark.backend, pytest.mark.unit]

A_TURNS = [
    {"role": "user", "content": "Frage aus A"},
    {"role": "assistant", "content": "Antwort aus A"},
]


def _loaded(session_id: str) -> ConversationSessionState:
    state = ConversationSessionState()
    state.db_session_id = session_id
    state.conversation_history = list(A_TURNS)
    state.history_loaded = True
    state.history_watermark = 10
    return state


class TestBindSession:
    def test_another_id_drops_the_previous_conversation(self):
        state = _loaded("A")

        state.bind_session("F")

        assert state.db_session_id == "F"
        assert state.conversation_history == []
        assert state.history_loaded is False
        assert state.history_watermark is None
        # ... so the next message loads F's own history.
        assert history_needs_reload(
            history_loaded=state.history_loaded, known_session_id=state.db_session_id,
            wanted_session_id="F", watermark=state.history_watermark, newest_in_db=None,
        )

    def test_the_same_id_keeps_the_history(self):
        state = _loaded("A")

        state.bind_session("A")

        assert state.conversation_history == A_TURNS
        assert state.history_loaded is True
        assert state.history_watermark == 10

    def test_a_first_bind_is_a_plain_set(self):
        state = ConversationSessionState()
        state.bind_session("A")
        assert state.db_session_id == "A"
        assert state.history_loaded is False


@pytest.fixture
def seeded(monkeypatch):
    """A socket whose state already holds conversation A, loaded."""
    state = _loaded("A")

    async def _fake_auth(_ws, _token=None):
        return {"authenticated": True, "user_id": 42, "auth_method": "jwt"}

    class _AllowAll:
        def check(self, _ip):
            return True, ""

    monkeypatch.setattr(ch, "authenticate_websocket", _fake_auth)
    monkeypatch.setattr(ch, "get_rate_limiter", lambda: _AllowAll())
    monkeypatch.setattr(ch, "ConversationSessionState", lambda: state)
    app = FastAPI()
    app.state.ollama = SimpleNamespace(default_lang="de")
    app.include_router(ch.router)
    return state, TestClient(app)


def _settle(ws) -> None:
    """Frames are handled in order: an answered invalid frame proves the
    register frame before it was processed."""
    ws.send_json({"type": "nonsense"})
    assert ws.receive_json()["type"] == "error"


def test_register_of_a_deleted_conversation_does_not_carry_the_old_history(
    seeded, monkeypatch,
):
    state, client = seeded
    # B was deleted: the boundary swaps it for the fresh F.
    replace = AsyncMock(return_value="F")
    monkeypatch.setattr(ch, "_replacement_session_for", replace)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "register", "session_id": "B"})
        _settle(ws)

    assert replace.await_args.args[1:] == ("B", 42)
    assert state.db_session_id == "F"
    assert state.conversation_history == []
    assert state.history_loaded is False
    assert state.history_watermark is None


def test_register_of_another_existing_conversation_drops_the_old_history(
    seeded, monkeypatch,
):
    """Not only #1485: any switch whose newest message is below A's watermark
    skipped the reload the same way."""
    state, client = seeded
    monkeypatch.setattr(ch, "_replacement_session_for", AsyncMock(return_value=None))

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "register", "session_id": "C"})
        _settle(ws)

    assert state.db_session_id == "C"
    assert state.conversation_history == []
    assert state.history_loaded is False


def test_register_of_the_same_conversation_keeps_its_history(seeded, monkeypatch):
    """A reconnecting tab re-registers its own id; that must not force a reload."""
    state, client = seeded
    monkeypatch.setattr(ch, "_replacement_session_for", AsyncMock(return_value=None))

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "register", "session_id": "A"})
        _settle(ws)

    assert state.conversation_history == A_TURNS
    assert state.history_loaded is True
