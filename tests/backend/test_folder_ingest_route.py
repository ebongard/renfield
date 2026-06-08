"""Endpoint tests for POST /api/folder-ingest/document (T3).

Exercises the status-code contract the dedicated filesystem MCP depends on:
503→retry (disabled / worker-down), 401/403 (token, fatal in the MCP), and the
4-state 200 body (ingested|duplicate|retry|failed). The bridge itself
(folder_ingest.ingest_document) is unit-tested separately in
test_folder_ingest.py; here it is mocked at the route boundary except for the
extension-reject case, which proves the real route→bridge wiring.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from slowapi.errors import RateLimitExceeded
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import SETTING_FOLDER_INGEST_TOKEN, SystemSetting
from services.folder_ingest import (
    FOLDER_INGEST_CONTRACT_VERSION,
    IngestResult,
    IngestStatus,
)

URL = "/api/folder-ingest/document"
TEST_TOKEN = "folder-ingest-test-token-abc123"


@pytest.fixture
async def client(db_session: AsyncSession):
    """A self-contained app mounting ONLY the folder-ingest router, with get_db
    overridden to the test session. Avoids importing the whole app (the build
    box's source tree can sit behind HEAD), keeping these route tests isolated
    to the endpoint under test."""
    from api.routes import folder_ingest as route
    from services.api_rate_limiter import limiter, rate_limit_exceeded_handler
    from services.database import get_db

    app = FastAPI()
    app.state.limiter = limiter  # the @limiter.limit decorator reads this
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(route.router, prefix="/api/folder-ingest")

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _auth(token: str = TEST_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _multipart(filename: str = "invoice.pdf", content: bytes = b"%PDF-1.4 hello", **meta):
    md = {"filename": filename, "root": "inbox", "relpath": filename}
    md.update(meta)
    return {
        "files": {"file": (filename, content, "application/pdf")},
        "data": {"metadata": json.dumps(md)},
    }


@pytest.fixture
async def token_set(db_session: AsyncSession) -> str:
    db_session.add(SystemSetting(key=SETTING_FOLDER_INGEST_TOKEN, value=TEST_TOKEN))
    await db_session.commit()
    return TEST_TOKEN


@pytest.fixture(autouse=True)
def _feature_enabled(request, monkeypatch):
    """The route 503s when folder_ingest_enabled is false (and the ambient
    .env ships it off). Force it on except for the test that asserts the
    disabled path."""
    if request.node.name == "test_503_when_disabled":
        return
    from api.routes import folder_ingest as route

    monkeypatch.setattr(route.settings, "folder_ingest_enabled", True)


@pytest.fixture(autouse=True)
def _worker_alive(request, monkeypatch):
    """Pretend the document worker heartbeat is fresh, except where a test
    drives the worker-down path."""
    if request.node.name == "test_503_when_worker_down":
        return
    from api.routes import folder_ingest as route

    monkeypatch.setattr(route, "_worker_is_alive", AsyncMock(return_value=True))


@pytest.mark.integration
async def test_503_when_disabled(client: AsyncClient, token_set: str):
    from api.routes import folder_ingest as route

    # explicit: feature off
    with patch.object(route.settings, "folder_ingest_enabled", False):
        r = await client.post(URL, **_multipart(), headers=_auth())
    assert r.status_code == 503
    assert r.json()["detail"]["reason"] == "feature_disabled"


@pytest.mark.integration
async def test_401_missing_auth(client: AsyncClient, token_set: str):
    r = await client.post(URL, **_multipart())  # no Authorization header
    assert r.status_code == 401


@pytest.mark.integration
async def test_403_wrong_token(client: AsyncClient, token_set: str):
    r = await client.post(URL, **_multipart(), headers=_auth("nope"))
    assert r.status_code == 403


@pytest.mark.integration
async def test_403_when_no_token_provisioned(client: AsyncClient):
    # No SystemSetting row at all → verify returns False → 403 (not a 500).
    r = await client.post(URL, **_multipart(), headers=_auth("anything"))
    assert r.status_code == 403


@pytest.mark.integration
async def test_503_when_worker_down(client: AsyncClient, token_set: str, monkeypatch):
    from api.routes import folder_ingest as route

    monkeypatch.setattr(route, "_worker_is_alive", AsyncMock(return_value=False))
    r = await client.post(URL, **_multipart(), headers=_auth())
    assert r.status_code == 503
    assert r.json()["detail"]["reason"] == "worker_unavailable"


@pytest.mark.integration
async def test_malformed_metadata_is_failed(client: AsyncClient, token_set: str):
    r = await client.post(
        URL,
        files={"file": ("x.pdf", b"%PDF", "application/pdf")},
        data={"metadata": "this is not json"},
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == IngestStatus.FAILED.value
    assert body["detail"] == "malformed_metadata"
    assert body["contract_version"] == FOLDER_INGEST_CONTRACT_VERSION


@pytest.mark.integration
async def test_oversize_streams_to_failed(client: AsyncClient, token_set: str, monkeypatch):
    from api.routes import folder_ingest as route

    monkeypatch.setattr(route.settings, "max_file_size_mb", 1)
    big = b"x" * (2 * 1024 * 1024)  # 2 MiB > 1 MiB ceiling
    r = await client.post(
        URL, **_multipart(content=big), headers=_auth()
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == IngestStatus.FAILED.value
    assert body["detail"] == "file_too_large"


@pytest.mark.integration
async def test_bad_extension_is_failed_real_bridge(client: AsyncClient, token_set: str):
    # No mock of the bridge: the real ingest_document rejects the extension
    # before any disk/DB work, proving the route→bridge wiring + 4-state map.
    r = await client.post(URL, **_multipart(filename="malware.exe"), headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == IngestStatus.FAILED.value
    assert body["detail"] == "extension_not_allowed"


@pytest.mark.integration
async def test_happy_path_ingested(client: AsyncClient, token_set: str, monkeypatch):
    from api.routes import folder_ingest as route

    fake = AsyncMock(
        return_value=IngestResult(IngestStatus.INGESTED, document_id=99, detail="enqueued")
    )
    monkeypatch.setattr(route, "ingest_document", fake)
    r = await client.post(URL, **_multipart(), headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ingested"
    assert body["document_id"] == 99
    assert body["contract_version"] == FOLDER_INGEST_CONTRACT_VERSION
    # the route resolved a server-side KB + ownerless enqueue and passed no leg
    _, kwargs = fake.await_args
    assert kwargs["kb_id"] is not None
    assert kwargs["paperless_leg"] is None


@pytest.mark.integration
async def test_retry_state_passthrough(client: AsyncClient, token_set: str, monkeypatch):
    from api.routes import folder_ingest as route

    monkeypatch.setattr(
        route,
        "ingest_document",
        AsyncMock(return_value=IngestResult(IngestStatus.RETRY, document_id=5, detail="in_progress")),
    )
    r = await client.post(URL, **_multipart(), headers=_auth())
    assert r.status_code == 200
    assert r.json()["status"] == "retry"


@pytest.mark.integration
async def test_client_kb_override_ignored(
    client: AsyncClient, token_set: str, db_session: AsyncSession, monkeypatch
):
    # A pusher that sticks knowledge_base_id in metadata cannot redirect the
    # ingest: IngestMeta drops it and the route resolves the KB server-side.
    from sqlalchemy import select

    from api.routes import folder_ingest as route
    from models.database import KnowledgeBase

    fake = AsyncMock(
        return_value=IngestResult(IngestStatus.INGESTED, document_id=1)
    )
    monkeypatch.setattr(route, "ingest_document", fake)
    r = await client.post(
        URL, **_multipart(knowledge_base_id=999999), headers=_auth()
    )
    assert r.status_code == 200
    # Prove it positively: kb_id is the id of the server-side configured KB,
    # not the client's 999999 (a bare `!= 999999` would pass trivially).
    expected_kb = (
        await db_session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.name == route.settings.folder_ingest_kb_name
            )
        )
    ).scalar_one()
    _, kwargs = fake.await_args
    assert kwargs["kb_id"] == expected_kb.id


@pytest.mark.integration
async def test_unexpected_error_is_503_not_500(client: AsyncClient, token_set: str, monkeypatch):
    # An unexpected failure in the bridge (e.g. Redis enqueue down) must surface
    # as 503/retry so the MCP re-pushes — never a raw 500 that breaks the
    # transport contract.
    from api.routes import folder_ingest as route

    monkeypatch.setattr(
        route, "ingest_document", AsyncMock(side_effect=RuntimeError("redis down"))
    )
    r = await client.post(URL, **_multipart(), headers=_auth())
    assert r.status_code == 503
    assert r.json()["detail"]["reason"] == "internal_error"
