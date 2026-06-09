"""Tests for email-mailbox auto-ingest (Phase 1).

Two layers:
- the `email_ingest` bridge (server-authoritative per-mailbox routing, the token
  helpers, the wrapper over the reused `ingest_document`);
- the `POST /api/email-ingest/document` route status-code + 4-state contract.

The folder-ingest bridge it delegates to is covered by test_folder_ingest*.py;
here it is mocked at the wrapper/route boundary except where wiring is asserted.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from slowapi.errors import RateLimitExceeded
from sqlalchemy.ext.asyncio import AsyncSession

import services.email_ingest as svc
from models.database import SETTING_EMAIL_INGEST_TOKEN, SystemSetting
from services.email_ingest import (
    EMAIL_INGEST_CONTRACT_VERSION,
    MailboxTarget,
    ingest_email_document,
    resolve_mailbox_target,
)
from services.folder_ingest import IngestResult, IngestStatus

pytestmark = [pytest.mark.unit]

URL = "/api/email-ingest/document"
TEST_TOKEN = "email-ingest-test-token-abc123"

_MAILBOXES = [
    {"id": "buchhaltung-xidra", "owner": "admin", "tier": 2, "kb": "x-idra Buchhaltung"},
    {"id": "privat", "owner": "", "tier": 0, "kb": "Privat Eingang"},
    {"id": "clamp", "owner": "", "tier": 9, "kb": ""},  # tier over-range + empty kb
]


# ---------------------------------------------------------------------------
# resolve_mailbox_target — server-authoritative routing (pure)
# ---------------------------------------------------------------------------

def _patch_mailboxes(monkeypatch, boxes=_MAILBOXES):
    # email_ingest_mailboxes is now a parsed property over the JSON string field;
    # set the backing field so the graceful-parse path is exercised too.
    monkeypatch.setattr(svc.settings, "email_ingest_mailboxes_json", json.dumps(boxes))


def test_resolve_known_mailbox(monkeypatch):
    _patch_mailboxes(monkeypatch)
    t = resolve_mailbox_target("buchhaltung-xidra")
    assert t == MailboxTarget("buchhaltung-xidra", "admin", 2, "x-idra Buchhaltung")


def test_resolve_unknown_mailbox_is_none(monkeypatch):
    _patch_mailboxes(monkeypatch)
    assert resolve_mailbox_target("does-not-exist") is None


def test_resolve_empty_id_is_none(monkeypatch):
    _patch_mailboxes(monkeypatch)
    assert resolve_mailbox_target("") is None
    assert resolve_mailbox_target("   ") is None


def test_resolve_clamps_tier_and_defaults_kb(monkeypatch):
    _patch_mailboxes(monkeypatch)
    t = resolve_mailbox_target("clamp")
    assert t.tier == 4  # 9 clamped to the 0-4 ladder
    assert t.kb_name == "Eingang"  # empty kb → default


def test_resolve_skips_malformed_entry_before_valid(monkeypatch):
    # A bad entry (non-numeric tier, a non-dict) must NOT crash routing for a
    # VALID mailbox that sits after it in the list.
    boxes = [
        {"id": "bad", "owner": "", "tier": "abc", "kb": "X"},  # tier int() raises
        "not-a-dict",  # entry.get() would raise
        {"id": "good", "owner": "", "tier": 1, "kb": "Good KB"},
    ]
    _patch_mailboxes(monkeypatch, boxes)
    assert resolve_mailbox_target("good") == MailboxTarget("good", "", 1, "Good KB")
    assert resolve_mailbox_target("bad") is None  # the malformed one is skipped


def test_config_property_graceful_on_bad_json(monkeypatch):
    # A malformed EMAIL_INGEST_MAILBOXES_JSON must fall back to [] (never crash).
    monkeypatch.setattr(svc.settings, "email_ingest_mailboxes_json", "not json{")
    assert svc.settings.email_ingest_mailboxes == []
    monkeypatch.setattr(svc.settings, "email_ingest_mailboxes_json", '{"not":"a list"}')
    assert svc.settings.email_ingest_mailboxes == []
    monkeypatch.setattr(svc.settings, "email_ingest_mailboxes_json", "")
    assert svc.settings.email_ingest_mailboxes == []


# ---------------------------------------------------------------------------
# ingest_email_document — wrapper routing + the unknown-mailbox guard
# ---------------------------------------------------------------------------

async def test_unknown_mailbox_is_failed_without_touching_db(monkeypatch):
    _patch_mailboxes(monkeypatch)
    db = AsyncMock()  # must NOT be used on the reject path
    result = await ingest_email_document(
        b"%PDF-1.4", db=db, mailbox_id="forged", message_id="m1", filename="x.pdf"
    )
    assert result.status is IngestStatus.FAILED
    assert result.detail == "unknown_mailbox"
    db.execute.assert_not_called()


async def test_known_mailbox_routes_sphere_to_ingest_document(monkeypatch):
    # Wiring: the resolved (kb_id, owner, tier) reach ingest_document; the
    # watcher never supplies them. KB/owner resolution + ledger are stubbed so
    # this isolates the routing wiring.
    _patch_mailboxes(monkeypatch)
    monkeypatch.setattr(svc, "_resolve_kb", AsyncMock(return_value=MagicMock(id=42)))
    monkeypatch.setattr(svc, "_resolve_owner", AsyncMock(return_value=7))
    monkeypatch.setattr(svc, "_record_ledger", AsyncMock())
    ingest = AsyncMock(return_value=IngestResult(IngestStatus.INGESTED, document_id=99))
    monkeypatch.setattr(svc, "ingest_document", ingest)

    out = await ingest_email_document(
        b"%PDF-1.4", db=AsyncMock(), mailbox_id="buchhaltung-xidra",
        message_id="m1", filename="rechnung.pdf",
    )
    assert out.status is IngestStatus.INGESTED and out.document_id == 99
    _, kwargs = ingest.call_args
    assert kwargs["kb_id"] == 42
    assert kwargs["owner_user_id"] == 7
    assert kwargs["default_tier"] == 2  # the mailbox's tier, server-side


async def test_ledger_recorded_with_provenance(monkeypatch):
    _patch_mailboxes(monkeypatch)
    monkeypatch.setattr(svc, "_resolve_kb", AsyncMock(return_value=MagicMock(id=1)))
    monkeypatch.setattr(svc, "_resolve_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "ingest_document",
                        AsyncMock(return_value=IngestResult(IngestStatus.INGESTED, document_id=5)))
    rec = AsyncMock()
    monkeypatch.setattr(svc, "_record_ledger", rec)

    await ingest_email_document(
        b"%PDF-1.4", db=AsyncMock(), mailbox_id="privat", message_id="<msg-7@x>",
        filename="bill.pdf", sender="vendor@acme.de", subject="Invoice 7",
    )
    _, k = rec.call_args
    assert k["mailbox_id"] == "privat" and k["message_id"] == "<msg-7@x>"
    assert k["sender"] == "vendor@acme.de" and k["subject"] == "Invoice 7"
    assert len(k["attachment_sha256"]) == 64  # content hash when no sha supplied


# ---------------------------------------------------------------------------
# token helpers (real PG via db_session)
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_token_generate_then_verify_roundtrip(db_session: AsyncSession):
    assert await svc.verify_email_ingest_token(db_session, "anything") is False  # unset
    tok = await svc.generate_email_ingest_token(db_session)
    assert await svc.verify_email_ingest_token(db_session, tok) is True
    assert await svc.verify_email_ingest_token(db_session, "wrong") is False


# ---------------------------------------------------------------------------
# route: POST /api/email-ingest/document — status + 4-state contract
# ---------------------------------------------------------------------------

@pytest.fixture
async def client(db_session: AsyncSession):
    from api.routes import email_ingest as route
    from services.api_rate_limiter import limiter, rate_limit_exceeded_handler
    from services.auth_service import get_current_user
    from services.database import get_db

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(route.router, prefix="/api/email-ingest")

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: MagicMock(id=1, username="admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _auth(token: str = TEST_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _multipart(filename="invoice.pdf", content=b"%PDF-1.4 hello", mailbox_id="privat", **meta):
    md = {"filename": filename, "mailbox_id": mailbox_id, "message_id": "<m1@x>"}
    md.update(meta)
    return {
        "files": {"file": (filename, content, "application/pdf")},
        "data": {"metadata": json.dumps(md)},
    }


@pytest.fixture
async def token_set(db_session: AsyncSession) -> str:
    db_session.add(SystemSetting(key=SETTING_EMAIL_INGEST_TOKEN, value=TEST_TOKEN))
    await db_session.commit()
    return TEST_TOKEN


@pytest.fixture(autouse=True)
def _feature_enabled(request, monkeypatch):
    if request.node.name == "test_503_when_disabled":
        return
    from api.routes import email_ingest as route
    monkeypatch.setattr(route.settings, "email_ingest_enabled", True)


@pytest.fixture(autouse=True)
def _worker_alive(request, monkeypatch):
    if request.node.name == "test_503_when_worker_down":
        return
    from api.routes import email_ingest as route
    monkeypatch.setattr(route, "_worker_is_alive", AsyncMock(return_value=True))


@pytest.mark.integration
async def test_503_when_disabled(client, token_set):
    from api.routes import email_ingest as route
    with patch.object(route.settings, "email_ingest_enabled", False):
        r = await client.post(URL, **_multipart(), headers=_auth())
    assert r.status_code == 503 and r.json()["detail"]["reason"] == "feature_disabled"


@pytest.mark.integration
async def test_401_missing_auth(client, token_set):
    assert (await client.post(URL, **_multipart())).status_code == 401


@pytest.mark.integration
async def test_403_wrong_token(client, token_set):
    assert (await client.post(URL, **_multipart(), headers=_auth("nope"))).status_code == 403


@pytest.mark.integration
async def test_403_when_no_token_provisioned(client):
    assert (await client.post(URL, **_multipart(), headers=_auth("anything"))).status_code == 403


@pytest.mark.integration
async def test_503_when_worker_down(client, token_set, monkeypatch):
    from api.routes import email_ingest as route
    monkeypatch.setattr(route, "_worker_is_alive", AsyncMock(return_value=False))
    r = await client.post(URL, **_multipart(), headers=_auth())
    assert r.status_code == 503 and r.json()["detail"]["reason"] == "worker_unavailable"


@pytest.mark.integration
async def test_malformed_metadata_is_failed(client, token_set):
    r = await client.post(
        URL, files={"file": ("x.pdf", b"%PDF", "application/pdf")},
        data={"metadata": "not json"}, headers=_auth(),
    )
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert r.json()["detail"] == "malformed_metadata"


@pytest.mark.integration
async def test_missing_mailbox_id_is_failed(client, token_set):
    md = {"filename": "x.pdf", "message_id": "<m@x>"}  # no mailbox_id
    r = await client.post(
        URL, files={"file": ("x.pdf", b"%PDF", "application/pdf")},
        data={"metadata": json.dumps(md)}, headers=_auth(),
    )
    assert r.status_code == 200 and r.json()["status"] == "failed"


@pytest.mark.integration
async def test_oversize_is_failed(client, token_set, monkeypatch):
    from api.routes import email_ingest as route
    monkeypatch.setattr(route.settings, "max_file_size_mb", 0)  # any non-empty body trips it
    r = await client.post(URL, **_multipart(content=b"x" * 2048), headers=_auth())
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert r.json()["detail"] == "file_too_large"


@pytest.mark.integration
async def test_happy_path_returns_ingested(client, token_set, monkeypatch):
    from api.routes import email_ingest as route
    monkeypatch.setattr(
        route, "ingest_email_document",
        AsyncMock(return_value=IngestResult(IngestStatus.INGESTED, document_id=5, detail="enqueued")),
    )
    r = await client.post(URL, **_multipart(), headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ingested" and body["document_id"] == 5
    assert body["contract_version"] == EMAIL_INGEST_CONTRACT_VERSION


@pytest.mark.integration
async def test_unknown_mailbox_routes_to_failed(client, token_set, monkeypatch):
    # End-to-end through the real wrapper (mailboxes table empty) → unknown → failed.
    monkeypatch.setattr(svc.settings, "email_ingest_mailboxes_json", "[]")
    r = await client.post(URL, **_multipart(mailbox_id="ghost"), headers=_auth())
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert r.json()["detail"] == "unknown_mailbox"


@pytest.mark.integration
async def test_health_reports_known_mailbox_ids(client, token_set, monkeypatch):
    monkeypatch.setattr(svc.settings, "email_ingest_mailboxes_json", json.dumps(_MAILBOXES))
    r = await client.get("/api/email-ingest/health", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert set(body["mailbox_ids"]) == {"buchhaltung-xidra", "privat", "clamp"}
    assert body["token_ok"] is True


@pytest.mark.integration
async def test_health_401_without_token(client):
    assert (await client.get("/api/email-ingest/health")).status_code == 401


@pytest.mark.integration
async def test_token_mint_returns_token(client):
    r = await client.post("/api/email-ingest/token")
    assert r.status_code == 200 and len(r.json()["token"]) > 20
