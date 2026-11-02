"""A button scan's outcome reaches the document owner's configured conversation (#1390).

A button press has no turn, so no requester is ever recorded for it. The fixed
recipient `SCANNER_UNATTENDED_RECIPIENT_<CLIENT_ID>=<user_id>:<session_id>` is
keyed by the AUTHENTICATED ingest client and must be the owner that client files
documents for -- against a real Postgres, because ownership is the point.
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from loguru import logger as loguru_logger

from services import scanner_jobs as sj

pytestmark = [pytest.mark.database]

JOB_ID = "cd" * 16
OWNER = 7
STRANGER = 8
OWN_SESSION = "owner-inbox"
FOREIGN_SESSION = "stranger-chat"
RECIPIENT_ENV = "SCANNER_UNATTENDED_RECIPIENT_SCANNER_HH"
ROOM_ENV = "SCANNER_UNATTENDED_ROOM_SCANNER_HH"
TITLE_FROM_EVENT = "Kontoauszug Geheim"


def _client(owner="nutzer7"):
    return SimpleNamespace(client_id="scanner-hh", label="Scanner", route="folder", owner=owner)


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

    async def eval(self, script, numkeys, *keys_and_args):
        key, token = keys_and_args
        if self.store.get(key) == token:
            self.store.pop(key)
            return 1
        return 0

    async def publish(self, channel, payload):
        self.published.append((channel, payload))


def _event(caller="button", status="done", **extra):
    return {
        "job_id": JOB_ID, "status": status, "caller": caller, "title": TITLE_FROM_EVENT,
        "result": {"ok": True, "target": "household", "pages": 5, "renfield_document_id": 613},
        **extra,
    }


@contextmanager
def _captured_logs(level="INFO"):
    lines: list[str] = []
    sink = loguru_logger.add(lambda m: lines.append(str(m)), level=level)
    try:
        yield lines
    finally:
        loguru_logger.remove(sink)


class _Hooks:
    """Records `announce_in_room` only; `save_message`'s own hook gets no plugin."""

    def __init__(self):
        self.announcements: list[dict] = []

    async def __call__(self, name, **kwargs):
        if name == "announce_in_room":
            self.announcements.append(kwargs)
        return []


@pytest.fixture
def session_maker(async_engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def household(session_maker, make_user, monkeypatch):
    """Owner 7 with their own conversation, stranger 8 with theirs."""
    from models.database import Conversation

    await make_user(OWNER)
    await make_user(STRANGER)
    async with session_maker() as db:
        db.add(Conversation(session_id=OWN_SESSION, user_id=OWNER))
        db.add(Conversation(session_id=FOREIGN_SESSION, user_id=STRANGER))
        await db.commit()
    monkeypatch.setattr(sj.settings, "auth_enabled", True)
    monkeypatch.setattr(sj.settings, "default_language", "de")
    monkeypatch.delenv(RECIPIENT_ENV, raising=False)
    monkeypatch.delenv(ROOM_ENV, raising=False)


async def _messages(session_maker, session_id):
    from sqlalchemy import select

    from models.database import Conversation, Message

    async with session_maker() as db:
        rows = await db.execute(
            select(Message).join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.session_id == session_id)
        )
        return list(rows.scalars())


async def _deliver(session_maker, redis, event=None, client=None):
    async with session_maker() as db:
        return await sj.handle_job_event(
            db, event or _event(), redis=redis, ingest_client=client or _client(),
        )


async def test_a_button_scan_lands_once_in_the_owners_conversation(
    household, session_maker, monkeypatch,
):
    monkeypatch.setenv(RECIPIENT_ENV, f"{OWNER}:{OWN_SESSION}")
    monkeypatch.setenv(ROOM_ENV, "3")
    redis, hooks = FakeRedis(), _Hooks()
    # 🛑 Identity in the body is never used -- only the token's client counts.
    forged = _event(user_id=STRANGER, session_id=FOREIGN_SESSION)
    with patch("utils.hooks.run_hooks", hooks):
        assert await _deliver(session_maker, redis, forged) == "delivered"
        # The scanner retries a lost reply: still one message, no second announcement.
        assert await _deliver(session_maker, redis) == "duplicate"
        # The Redis marker lost too: the message itself is the durable record.
        del redis.store[f"renfield:scanner:job:{JOB_ID}:reported"]
        assert await _deliver(session_maker, redis) == "duplicate"

    messages = await _messages(session_maker, OWN_SESSION)
    assert len(messages) == 1
    message = messages[0]
    assert message.role == "assistant"
    assert message.message_metadata["scanner_job"] == {"job_id": JOB_ID, "status": "done"}
    # Fixed template only: the event's title never reaches the chat.
    assert TITLE_FROM_EVENT not in message.content
    assert message.content == sj.render_completion_message(
        "done", "", _event()["result"], "de")
    assert await _messages(session_maker, FOREIGN_SESSION) == []

    # Spoken in the configured room: page count, no title, no document detail.
    assert hooks.announcements == [{"room_id": 3, "text": "Der Scan ist fertig, 5 Seite(n)."}]
    assert "613" not in hooks.announcements[0]["text"]
    _channel, payload = redis.published[0]
    assert json.loads(payload) == {"target": OWNER, "type": "scan_job_finished",
                                   "reason": "done", "session_id": OWN_SESSION}
    assert len(redis.published) == 1


async def test_a_failed_button_scan_is_announced_without_a_count(
    household, session_maker, monkeypatch,
):
    monkeypatch.setenv(RECIPIENT_ENV, f"{OWNER}:{OWN_SESSION}")
    monkeypatch.setenv(ROOM_ENV, "3")
    hooks = _Hooks()
    event = {**_event(status="failed"), "result": {"ok": False, "error_code": "no_pages"}}
    with patch("utils.hooks.run_hooks", hooks):
        assert await _deliver(session_maker, FakeRedis(), event) == "delivered"
    assert hooks.announcements == [{"room_id": 3, "text": "Der Scan ist fehlgeschlagen."}]
    [message] = await _messages(session_maker, OWN_SESSION)
    assert message.message_metadata["action_success"] is False


async def test_without_a_room_there_is_no_announcement(household, session_maker, monkeypatch):
    monkeypatch.setenv(RECIPIENT_ENV, f"{OWNER}:{OWN_SESSION}")
    hooks = _Hooks()
    with patch("utils.hooks.run_hooks", hooks):
        assert await _deliver(session_maker, FakeRedis()) == "delivered"
    assert hooks.announcements == []
    assert len(await _messages(session_maker, OWN_SESSION)) == 1


async def test_a_malformed_room_is_named_and_skipped(household, session_maker, monkeypatch):
    monkeypatch.setenv(RECIPIENT_ENV, f"{OWNER}:{OWN_SESSION}")
    monkeypatch.setenv(ROOM_ENV, "kueche")
    hooks = _Hooks()
    with _captured_logs("WARNING") as logs, patch("utils.hooks.run_hooks", hooks):
        assert await _deliver(session_maker, FakeRedis()) == "delivered"
    assert hooks.announcements == []
    assert any("scanner-hh" in line and "not a room id" in line for line in logs), logs


async def test_a_recipient_the_client_does_not_file_for_is_refused(
    household, session_maker, monkeypatch,
):
    """The configured user is not the documents' owner: refused, final, nothing written."""
    monkeypatch.setenv(RECIPIENT_ENV, f"{OWNER}:{OWN_SESSION}")
    monkeypatch.setenv(ROOM_ENV, "3")
    redis, hooks = FakeRedis(), _Hooks()
    with _captured_logs("WARNING") as logs, patch("utils.hooks.run_hooks", hooks):
        outcome = await _deliver(session_maker, redis, client=_client(owner="nutzer8"))
    assert outcome == "refused"
    assert await _messages(session_maker, OWN_SESSION) == []
    assert redis.published == [] and hooks.announcements == []
    assert any("refused" in line and "scanner-hh" in line for line in logs), logs


@pytest.mark.parametrize("session_id", [FOREIGN_SESSION, "does-not-exist"])
async def test_a_conversation_the_owner_does_not_own_is_refused(
    household, session_maker, monkeypatch, session_id,
):
    from sqlalchemy import func, select

    from models.database import Conversation

    monkeypatch.setenv(RECIPIENT_ENV, f"{OWNER}:{session_id}")
    redis, hooks = FakeRedis(), _Hooks()
    with _captured_logs("WARNING") as logs, patch("utils.hooks.run_hooks", hooks):
        assert await _deliver(session_maker, redis) == "refused"
    assert await _messages(session_maker, FOREIGN_SESSION) == []
    assert await _messages(session_maker, OWN_SESSION) == []
    assert redis.published == [] and hooks.announcements == []
    assert any("not owned by user 7" in line for line in logs), logs
    async with session_maker() as db:
        # A missing conversation is never created by the fallback.
        assert await db.scalar(select(func.count()).select_from(Conversation)) == 2


@pytest.mark.parametrize("value", ["", "7", "seven:s", "7:", ":s", "7:two words"])
async def test_no_valid_recipient_keeps_the_final_409_and_names_the_client(
    household, session_maker, monkeypatch, value,
):
    if value:
        monkeypatch.setenv(RECIPIENT_ENV, value)
    with _captured_logs("WARNING") as logs:
        assert await _deliver(session_maker, FakeRedis()) == "unknown_job"
    assert await _messages(session_maker, OWN_SESSION) == []
    assert any("scanner-hh" in line for line in logs), logs


async def test_a_chat_scan_never_takes_the_fallback(household, session_maker, monkeypatch):
    """A chat scan's outcome can outrun its requester record; it must keep waiting
    (409, retried) instead of landing in the configured conversation."""
    monkeypatch.setenv(RECIPIENT_ENV, f"{OWNER}:{OWN_SESSION}")
    for caller in ("", "household", "chat"):
        with _captured_logs("WARNING") as logs:
            assert await _deliver(session_maker, FakeRedis(), _event(caller=caller)) == "unknown_job"
        assert logs == [], caller
    assert await _messages(session_maker, OWN_SESSION) == []


async def test_a_recorded_requester_wins_over_the_fallback(household, session_maker, monkeypatch):
    """Even with `caller == "button"`, a recorded requester is the recipient."""
    monkeypatch.setenv(RECIPIENT_ENV, f"{OWNER}:{OWN_SESSION}")
    redis = FakeRedis()
    await sj.remember_scan_requester(
        {"success": True, "message": json.dumps({"ok": True, "job_id": JOB_ID})},
        user_id=STRANGER, session_id=FOREIGN_SESSION, title="Rechnung", redis=redis,
    )
    with patch("utils.hooks.run_hooks", _Hooks()):
        assert await _deliver(session_maker, redis) == "delivered"
    assert await _messages(session_maker, OWN_SESSION) == []
    [message] = await _messages(session_maker, FOREIGN_SESSION)
    assert "Rechnung" in message.content


async def test_without_an_ingest_client_nothing_falls_back(household, session_maker, monkeypatch):
    monkeypatch.setenv(RECIPIENT_ENV, f"{OWNER}:{OWN_SESSION}")
    async with session_maker() as db:
        assert await sj.handle_job_event(db, _event(), redis=FakeRedis()) == "unknown_job"
    assert await _messages(session_maker, OWN_SESSION) == []


def test_the_spoken_count_carries_no_title_or_document_detail():
    for lang in ("de", "en"):
        spoken = sj._TEXT[lang]["spoken"]["done_pages"]
        assert spoken.count("{") == 1 and "{pages}" in spoken


def test_the_client_id_maps_to_one_env_name():
    assert sj._client_env("X_{client}", "rfi-scanner") == ""
    with patch.dict("os.environ", {"X_RFI_SCANNER": "  7:s  "}):
        assert sj._client_env("X_{client}", "rfi-scanner") == "7:s"


# --- the route --------------------------------------------------------------------

@pytest.fixture
async def route_client(monkeypatch):
    from slowapi.errors import RateLimitExceeded

    from api.routes import scanner_jobs as route
    from services.api_rate_limiter import limiter, rate_limit_exceeded_handler
    from services.database import get_db

    monkeypatch.setattr(route.settings, "folder_ingest_enabled", True)
    monkeypatch.setattr(route.settings, "scanner_ingest_client_ids", "scanner-hh")
    monkeypatch.delenv(RECIPIENT_ENV, raising=False)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(route.router, prefix="/api/scanner")

    async def _db():
        yield MagicMock()

    app.dependency_overrides[get_db] = _db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _body(caller="button"):
    return {"contract_version": "1", "job_id": JOB_ID, "status": "done", "caller": caller,
            "result": {"ok": True, "pages": 2}}


async def test_the_route_hands_the_authenticated_client_to_the_service(route_client):
    handler = AsyncMock(return_value="delivered")
    client = _client()
    with patch("api.routes.scanner_jobs.resolve_folder_ingest_client", AsyncMock(return_value=client)), \
         patch("api.routes.scanner_jobs.handle_job_event", handler):
        resp = await route_client.post("/api/scanner/job-event", json=_body(),
                                       headers={"Authorization": "Bearer ok"})
    assert resp.status_code == 200
    assert handler.await_args.kwargs["ingest_client"] is client


async def test_an_unconfigured_button_scan_is_a_final_409_with_a_warning(route_client):
    """End to end through the real service: no recipient → the marker 409 of
    before, plus a WARNING that names the client."""
    with patch("api.routes.scanner_jobs.resolve_folder_ingest_client", AsyncMock(return_value=_client())), \
         patch("services.redis_client.get_redis", return_value=FakeRedis()), \
         _captured_logs("WARNING") as logs:
        resp = await route_client.post("/api/scanner/job-event", json=_body(),
                                       headers={"Authorization": "Bearer ok"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "no_recipient_for_caller"
    assert any("scanner-hh" in line and RECIPIENT_ENV.replace("SCANNER_HH", "<CLIENT_ID>") in line
               for line in logs), logs


async def test_a_refused_button_scan_is_final_2xx(route_client):
    handler = AsyncMock(return_value="refused")
    with patch("api.routes.scanner_jobs.resolve_folder_ingest_client", AsyncMock(return_value=_client())), \
         patch("api.routes.scanner_jobs.handle_job_event", handler):
        resp = await route_client.post("/api/scanner/job-event", json=_body(),
                                       headers={"Authorization": "Bearer ok"})
    assert resp.status_code == 200 and resp.json() == {"status": "refused"}
