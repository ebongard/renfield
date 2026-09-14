"""Scan-job return path (renfield-mcp-scanner job model).

The failure that motivated it, 2026-09-14: `scan_document` ran the whole scan inside
the MCP call, so the backend's 30s timeout reported a FAILED scan that had been
filed 80ms later. Now the call only starts a job; the outcome arrives as an event.
These tests pin the trust boundary (the requester comes from the authenticated
turn, never from the event), at-most-once delivery into the chat, and the reply
codes the scanner's retry logic depends on.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services import scanner_jobs as sj

pytestmark = [pytest.mark.unit]

JOB_ID = "ab" * 16


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)

    async def publish(self, channel, payload):
        self.published.append((channel, payload))


def _tool_result(body, success=True):
    return {"success": success, "message": json.dumps(body), "data": None}


# --- extracting the job id ---------------------------------------------------

def test_job_id_from_a_started_scan():
    assert sj.extract_job_id(_tool_result({"ok": True, "job_id": JOB_ID, "status": "running"})) == JOB_ID


@pytest.mark.parametrize(
    "result",
    [
        None,
        {"success": False, "message": "Tool-Aufruf Timeout", "data": None},
        {"success": True, "message": "not json", "data": None},
        _tool_result({"ok": False, "busy": True, "job_id": JOB_ID}),
        _tool_result({"ok": True, "job_id": "../../etc"}),
        _tool_result(["ok"]),
    ],
    ids=["none", "failed-call", "prose", "busy", "malformed-id", "not-an-object"],
)
def test_no_job_id_unless_a_scan_actually_started(result):
    """A `busy` reply names the RUNNING job, which already has its requester."""
    assert sj.extract_job_id(result) is None


# --- recording the requester ---------------------------------------------------

async def test_requester_is_recorded_from_the_turn():
    redis = FakeRedis()
    ok = await sj.remember_scan_requester(
        _tool_result({"ok": True, "job_id": JOB_ID}), user_id=7, session_id="session-1", redis=redis
    )
    assert ok
    assert json.loads(redis.store[f"renfield:scanner:job:{JOB_ID}"]) == {
        "user_id": 7, "session_id": "session-1",
    }


async def test_no_session_means_nothing_to_report_into():
    redis = FakeRedis()
    ok = await sj.remember_scan_requester(
        _tool_result({"ok": True, "job_id": JOB_ID}), user_id=7, session_id=None, redis=redis
    )
    assert not ok and redis.store == {}


async def test_redis_failure_never_breaks_the_scan():
    redis = FakeRedis()
    redis.set = AsyncMock(side_effect=ConnectionError("redis down"))
    ok = await sj.remember_scan_requester(
        _tool_result({"ok": True, "job_id": JOB_ID}), user_id=7, session_id="s", redis=redis
    )
    assert ok is False


# --- handling the event --------------------------------------------------------

def _event(status="done", **result):
    return {"job_id": JOB_ID, "status": status, "title": "",
            "result": result or {"ok": True, "target": "household", "pages": 5,
                                 "renfield_document_id": 613}}


@pytest.fixture
def conversation():
    service = MagicMock()
    service.save_message = AsyncMock(return_value=MagicMock(id=1))
    with patch("services.conversation_service.ConversationService", return_value=service):
        yield service


async def test_event_for_an_unrecorded_job_is_ignored(conversation):
    """No record = not ours, expired, or forged. Nothing is written anywhere."""
    redis = FakeRedis()
    assert await sj.handle_job_event(MagicMock(), _event(), redis=redis) == "unknown_job"
    conversation.save_message.assert_not_called()
    assert redis.published == []


async def test_event_lands_in_the_requesting_conversation(conversation, monkeypatch):
    monkeypatch.setattr(sj.settings, "ws_auth_enabled", True)
    monkeypatch.setattr(sj.settings, "default_language", "de")
    redis = FakeRedis()
    await sj.remember_scan_requester(
        _tool_result({"ok": True, "job_id": JOB_ID}), user_id=7, session_id="session-1", redis=redis
    )

    assert await sj.handle_job_event(MagicMock(), _event(), redis=redis) == "delivered"

    kwargs = conversation.save_message.await_args.kwargs
    assert kwargs["session_id"] == "session-1" and kwargs["user_id"] == 7
    assert kwargs["role"] == "assistant" and kwargs["enforce_ownership"] is True
    assert "613" in kwargs["content"] and "Paperless" in kwargs["content"]
    channel, payload = redis.published[0]
    assert json.loads(payload) == {"target": 7, "type": "scan_job_finished", "reason": "done"}


async def test_a_retried_event_is_written_once(conversation):
    redis = FakeRedis()
    await sj.remember_scan_requester(
        _tool_result({"ok": True, "job_id": JOB_ID}), user_id=7, session_id="s", redis=redis
    )
    assert await sj.handle_job_event(MagicMock(), _event(), redis=redis) == "delivered"
    assert await sj.handle_job_event(MagicMock(), _event(), redis=redis) == "duplicate"
    assert conversation.save_message.await_count == 1


async def test_a_failed_write_releases_the_claim_for_the_retry(conversation):
    redis = FakeRedis()
    await sj.remember_scan_requester(
        _tool_result({"ok": True, "job_id": JOB_ID}), user_id=7, session_id="s", redis=redis
    )
    conversation.save_message.side_effect = RuntimeError("db down")

    with pytest.raises(RuntimeError):
        await sj.handle_job_event(MagicMock(), _event(), redis=redis)

    conversation.save_message.side_effect = None
    assert await sj.handle_job_event(MagicMock(), _event(), redis=redis) == "delivered"


async def test_auth_off_pushes_to_the_household_bucket(conversation, monkeypatch):
    monkeypatch.setattr(sj.settings, "ws_auth_enabled", False)
    redis = FakeRedis()
    await sj.remember_scan_requester(
        _tool_result({"ok": True, "job_id": JOB_ID}), user_id=None, session_id="s", redis=redis
    )
    await sj.handle_job_event(MagicMock(), _event(), redis=redis)
    assert json.loads(redis.published[0][1])["target"] is None


# --- the message ---------------------------------------------------------------

def test_messages_never_claim_success_they_do_not_have():
    unrouted = sj.render_completion_message("unrouted", "", {"ok": True, "routed": False}, "de")
    assert "nichts wurde abgelegt" in unrouted
    interrupted = sj.render_completion_message("interrupted", "", {"ok": False}, "en")
    assert "Do not assume anything was filed" in interrupted
    failed = sj.render_completion_message("failed", "Rechnung", {"error": "x" * 1000}, "de")
    assert "fehlgeschlagen" in failed and len(failed) < 400


def test_split_scan_reports_the_piece_count():
    text = sj.render_completion_message(
        "done", "", {"ok": True, "split": True, "documents": [{}, {}, {}]}, "en"
    )
    assert "3 documents" in text


def test_unsupported_language_falls_back_to_english():
    assert "The scan" in sj.render_completion_message("done", "", {"pages": 1, "target": "t"}, "it")


# --- the route -----------------------------------------------------------------

@pytest.fixture
async def client():
    from api.routes import scanner_jobs as route
    from services.database import get_db

    app = FastAPI()
    app.include_router(route.router, prefix="/api/scanner")

    async def _db():
        yield MagicMock()

    app.dependency_overrides[get_db] = _db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _body(status="done"):
    return {"contract_version": "1", "job_id": JOB_ID, "status": status, "result": {"ok": True}}


async def test_route_rejects_a_bad_token(client):
    with patch("api.routes.scanner_jobs.resolve_folder_ingest_client", AsyncMock(return_value=None)):
        resp = await client.post("/api/scanner/job-event", json=_body(),
                                 headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 403


async def test_route_never_404s_an_unknown_job(client):
    """The scanner treats 404 as final: an unknown job must be a quiet 2xx."""
    with patch("api.routes.scanner_jobs.resolve_folder_ingest_client", AsyncMock(return_value=object())), \
         patch("api.routes.scanner_jobs.handle_job_event", AsyncMock(return_value="unknown_job")):
        resp = await client.post("/api/scanner/job-event", json=_body(),
                                 headers={"Authorization": "Bearer ok"})
    assert resp.status_code == 200 and resp.json() == {"status": "unknown_job"}


async def test_route_ignores_non_terminal_status(client):
    handler = AsyncMock()
    with patch("api.routes.scanner_jobs.resolve_folder_ingest_client", AsyncMock(return_value=object())), \
         patch("api.routes.scanner_jobs.handle_job_event", handler):
        resp = await client.post("/api/scanner/job-event", json=_body("running"),
                                 headers={"Authorization": "Bearer ok"})
    assert resp.status_code == 200 and resp.json()["status"] == "ignored"
    handler.assert_not_called()


async def test_route_rejects_a_malformed_job_id(client):
    with patch("api.routes.scanner_jobs.resolve_folder_ingest_client", AsyncMock(return_value=object())):
        resp = await client.post("/api/scanner/job-event",
                                 json={**_body(), "job_id": "../../x"},
                                 headers={"Authorization": "Bearer ok"})
    assert resp.status_code == 422


# --- the executor hook -----------------------------------------------------------

async def test_executor_records_the_requester_of_a_started_scan():
    from services.action_executor import ActionExecutor

    mcp = MagicMock()
    mcp.execute_tool = AsyncMock(return_value=_tool_result({"ok": True, "job_id": JOB_ID}))
    executor = ActionExecutor(mcp_manager=mcp, session_id="session-9")
    remember = AsyncMock(return_value=True)

    with patch("services.scanner_jobs.remember_scan_requester", remember):
        result = await executor.execute(
            {"intent": "mcp.scanner.scan_document", "parameters": {}}, user_id=42
        )

    assert result["success"] is True
    remember.assert_awaited_once()
    assert remember.await_args.kwargs == {"user_id": 42, "session_id": "session-9"}


async def test_executor_leaves_other_mcp_tools_alone():
    from services.action_executor import ActionExecutor

    mcp = MagicMock()
    mcp.execute_tool = AsyncMock(return_value={"success": True, "message": "{}", "data": None})
    executor = ActionExecutor(mcp_manager=mcp, session_id="s")
    remember = AsyncMock()

    with patch("services.scanner_jobs.remember_scan_requester", remember):
        await executor.execute({"intent": "mcp.scanner.scanner_status", "parameters": {}})

    remember.assert_not_called()
