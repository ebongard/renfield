"""The single-agent WebSocket path gates its final answer through check_output.

#1269 wired the gate into the orchestrator branch, the chat_stream loops, RAG,
the error send and REST -- and missed the single-agent loop, although the
issue's own repro (router -> one role, no orchestrator) was exactly that path.
Reva measured it again on 2026-11-10: its check_output handler (GDPR redaction,
the "first N shown" notice) never ran on single-agent web turns.

`test_output_gate.py` pins the helper; this file pins the CALL SITE by driving
a real WebSocket turn through `websocket_endpoint`.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.websocket.chat_handler as ch
from services.agent_router import AgentRole
from services.agent_service import AgentStep
from services.input_guard import InjectionResult
from utils import hooks
from utils.config import settings

# `database`: the turn opens its own sessions (conversation lookup, history),
# which `own_session_to_test_db` points at the test database.
pytestmark = [pytest.mark.backend, pytest.mark.database]

RAW = "Kunde Max Mustermann, IBAN DE02120300000000202051"
REDACTED = "Kunde [REDACTED], IBAN [REDACTED]"


class _FakeAgent:
    """Stands in for AgentService: one thinking step, then the raw answer."""

    def __init__(self, *_a, **_kw):
        pass

    async def run(self, **_kw):
        yield AgentStep(step_number=1, step_type="thinking", content="…")
        yield AgentStep(step_number=2, step_type="final_answer", content=RAW)


@pytest.fixture
def seen():
    return {}


@pytest.fixture
def client(monkeypatch, seen, own_session_to_test_db):
    async def _fake_auth(_ws, _token=None):
        return {"authenticated": True, "user_id": 42, "auth_method": "jwt"}

    class _AllowAll:
        def check(self, _ip):
            return True, ""

    monkeypatch.setattr(ch, "authenticate_websocket", _fake_auth)
    monkeypatch.setattr(ch, "get_rate_limiter", lambda: _AllowAll())
    monkeypatch.setattr(
        ch, "detect_injection",
        lambda _text: InjectionResult(score=0.0, blocked=False, matched_patterns=[]),
    )
    monkeypatch.setattr(settings, "agent_enabled", True)
    # Memory retrieval embeds the message against a live model (~18 s); the
    # gate does not depend on it.
    monkeypatch.setattr(ch, "_retrieve_memory_context", AsyncMock(return_value=""))

    import services.action_executor as ae
    import services.agent_service as asvc
    import services.agent_tools as at

    monkeypatch.setattr(asvc, "AgentService", _FakeAgent)
    monkeypatch.setattr(at.AgentToolRegistry, "create", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(ae, "ActionExecutor", lambda **_kw: MagicMock())

    role = AgentRole(name="jira", description={"de": "Jira"}, route_layer="semantic")
    router = SimpleNamespace(
        classify_with_context=AsyncMock(return_value=role),
        role_for_intent=lambda _i: None,
        roles={"jira": role},
    )

    # The plugin seam under test. `seen["crash"]` turns it into a redactor
    # that fails, to pin the fail-closed half at this call site.
    async def _check_output(content, role=None, user_id=None, **_kw):
        seen["check_output"] = {"content": content, "role": role, "user_id": user_id}
        if seen.get("crash"):
            raise RuntimeError("redactor down")
        return REDACTED

    # What the turn hands on after the send: memory extraction and the
    # post_message hooks (KG, plugins) both work from `full_response`.
    def _extract(*, assistant_response, **_kw):
        seen["extract_input"] = assistant_response
        return SimpleNamespace(task=None, owns_post_message=False)

    def _post_message(*, assistant_msg, **_kw):
        seen["post_message_input"] = assistant_msg

    monkeypatch.setattr(ch, "spawn_memory_extraction", _extract)
    monkeypatch.setattr(ch, "spawn_post_message_hooks", _post_message)

    saved = dict(hooks._hooks)
    hooks._hooks.clear()
    hooks.register_hook("check_output", _check_output)

    app = FastAPI()
    app.state.ollama = SimpleNamespace(default_lang="de")
    app.state.agent_router = router
    app.include_router(ch.router)
    try:
        yield TestClient(app)
    finally:
        hooks._hooks.clear()
        hooks._hooks.update(saved)


def _turn(ws) -> list[dict]:
    ws.send_json({"type": "text", "content": "Zeige mir offene Jira-Tickets"})
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame.get("type") == "done":
            return frames


def test_single_agent_answer_is_gated_before_it_is_sent(client, seen):
    with client.websocket_connect("/ws") as ws:
        frames = _turn(ws)

    sent = [f["content"] for f in frames if f.get("type") == "stream"]
    assert seen["check_output"] == {"content": RAW, "role": "jira", "user_id": 42}
    assert sent == [REDACTED]
    assert all(RAW not in str(f) for f in frames)


def test_later_hooks_see_the_gated_text(client, seen):
    with client.websocket_connect("/ws") as ws:
        _turn(ws)
    assert seen.get("extract_input") == REDACTED
    assert seen.get("post_message_input") == REDACTED


def test_a_crashing_redactor_sends_no_raw_text(client, seen):
    seen["crash"] = True
    with client.websocket_connect("/ws") as ws:
        frames = _turn(ws)

    sent = [f["content"] for f in frames if f.get("type") == "stream"]
    assert len(sent) == 1 and sent[0] not in (RAW, REDACTED)
    assert all(RAW not in str(f) for f in frames)
    assert seen.get("extract_input") == sent[0]
