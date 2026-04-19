"""Upload-endpoint routing tests for the document-worker cutover (#388, PR C1).

Matrix items:
  #2  upload with duplicate hash → 409 (route-level behaviour, unchanged by PR B)
  #6  upload with flag=on + worker heartbeat present → 202 + queued row
  #7  upload with flag=on + heartbeat missing → 503 + cleanup
  #8  upload with flag=off → 200 legacy inline path still works
  #14 GET /api/knowledge/documents/batch returns requested ids
"""
from __future__ import annotations

import io
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from models.database import DOC_STATUS_PENDING, Document, KnowledgeBase


@pytest.fixture
async def kb(db_session):
    """A knowledge base row to upload into."""
    row = KnowledgeBase(name="upload-test", description="fixture")
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


def _fake_upload(content: bytes = b"hallo welt", name: str = "hello.txt") -> tuple[str, tuple]:
    return ("file", (name, io.BytesIO(content), "text/plain"))


# ---------------------------------------------------------------------------
# Matrix #2 — duplicate hash returns 409 regardless of flag state
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.database
async def test_upload_duplicate_hash_returns_409(
    async_client: AsyncClient, db_session, kb
):
    """Uploading the same bytes twice to the same KB returns 409 with the
    existing_document payload — this is the contract the 409 dialog in the
    frontend reads from."""
    payload = b"duplicate-content-2026-04-19"

    # First upload succeeds (mock ingestion so we don't hit Docling).
    with patch(
        "services.rag_service.RAGService.ingest_document",
        new=AsyncMock(
            return_value=Document(
                id=1,
                filename="dup.txt",
                status="completed",
                knowledge_base_id=kb.id,
                file_hash="fixed",
            )
        ),
    ):
        response = await async_client.post(
            f"/api/knowledge/upload?knowledge_base_id={kb.id}",
            files=[_fake_upload(payload, "dup.txt")],
        )
    assert response.status_code == 200, response.text

    # Second upload of identical bytes → 409.
    response = await async_client.post(
        f"/api/knowledge/upload?knowledge_base_id={kb.id}",
        files=[_fake_upload(payload, "dup.txt")],
    )
    assert response.status_code == 409
    body = response.json()
    assert "existing_document" in body["detail"]
    assert body["detail"]["existing_document"]["filename"] == "dup.txt"


# ---------------------------------------------------------------------------
# Matrix #6 — flag ON + worker alive → 202 + enqueued + pending row
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.database
async def test_upload_returns_202_when_worker_enabled_and_alive(
    async_client: AsyncClient, db_session, kb, monkeypatch
):
    """With DOCUMENT_WORKER_ENABLED=true and the heartbeat present, upload
    should persist a pending Document and enqueue a task."""
    from utils.config import settings

    monkeypatch.setattr(settings, "document_worker_enabled", True)

    enqueued: list[dict] = []

    async def _fake_enqueue(self, params):
        enqueued.append(params)
        return "1699999999-0"

    with patch(
        "api.routes.knowledge._worker_is_alive",
        new=AsyncMock(return_value=True),
    ), patch(
        "api.routes.knowledge.DocumentTaskQueue.enqueue",
        new=_fake_enqueue,
    ):
        response = await async_client.post(
            f"/api/knowledge/upload?knowledge_base_id={kb.id}",
            files=[_fake_upload(b"neuer upload", "new.txt")],
        )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == DOC_STATUS_PENDING
    assert body["filename"] == "new.txt"
    assert body["id"] > 0
    # Enqueue payload matches the contract the worker consumes.
    assert len(enqueued) == 1
    assert enqueued[0]["document_id"] == body["id"]
    assert enqueued[0]["force_ocr"] is False


# ---------------------------------------------------------------------------
# Matrix #7 — flag ON + heartbeat missing → 503
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.database
async def test_upload_returns_503_when_worker_heartbeat_missing(
    async_client: AsyncClient, db_session, kb, monkeypatch
):
    """With flag on and no heartbeat, the endpoint must 503 (retryable)
    rather than silently enqueue into a stream nobody's reading."""
    from utils.config import settings

    monkeypatch.setattr(settings, "document_worker_enabled", True)

    enqueue_mock = AsyncMock()
    with patch(
        "api.routes.knowledge._worker_is_alive",
        new=AsyncMock(return_value=False),
    ), patch(
        "api.routes.knowledge.DocumentTaskQueue.enqueue",
        new=enqueue_mock,
    ):
        response = await async_client.post(
            f"/api/knowledge/upload?knowledge_base_id={kb.id}",
            files=[_fake_upload(b"worker down", "down.txt")],
        )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["detail"]["retryable"] is True
    # Must not have enqueued.
    enqueue_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Matrix #8 — flag OFF → legacy inline path (returns 200, doc processed)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.database
async def test_upload_legacy_inline_path_when_flag_off(
    async_client: AsyncClient, db_session, kb, monkeypatch
):
    """With the flag off (the current production default) the endpoint must
    still run the synchronous ingest path and return 200 — no worker touch."""
    from utils.config import settings

    monkeypatch.setattr(settings, "document_worker_enabled", False)

    worker_alive = AsyncMock()
    with patch(
        "services.rag_service.RAGService.ingest_document",
        new=AsyncMock(
            return_value=Document(
                id=42,
                filename="legacy.txt",
                status="completed",
                knowledge_base_id=kb.id,
            )
        ),
    ), patch(
        "api.routes.knowledge._worker_is_alive",
        new=worker_alive,
    ):
        response = await async_client.post(
            f"/api/knowledge/upload?knowledge_base_id={kb.id}",
            files=[_fake_upload(b"legacy payload", "legacy.txt")],
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    # The heartbeat check must be skipped entirely on the legacy path.
    worker_alive.assert_not_called()


# ---------------------------------------------------------------------------
# Matrix #14 — batch endpoint returns all ids
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.database
async def test_batch_endpoint_returns_requested_ids(
    async_client: AsyncClient, db_session, kb
):
    docs = [
        Document(filename=f"f{i}.txt", status="pending", knowledge_base_id=kb.id)
        for i in range(3)
    ]
    db_session.add_all(docs)
    await db_session.commit()
    for d in docs:
        await db_session.refresh(d)
    ids_csv = ",".join(str(d.id) for d in docs)

    response = await async_client.get(f"/api/knowledge/documents/batch?ids={ids_csv}")
    assert response.status_code == 200
    body = response.json()
    returned_ids = sorted(row["id"] for row in body)
    assert returned_ids == sorted(d.id for d in docs)


@pytest.mark.unit
async def test_batch_endpoint_rejects_oversize_batch(async_client: AsyncClient):
    ids_csv = ",".join(str(i) for i in range(1, 60))
    response = await async_client.get(f"/api/knowledge/documents/batch?ids={ids_csv}")
    assert response.status_code == 400
    assert "Batch too large" in response.json()["detail"]


@pytest.mark.unit
async def test_batch_endpoint_rejects_non_integer_ids(async_client: AsyncClient):
    response = await async_client.get("/api/knowledge/documents/batch?ids=1,abc,3")
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# C2: 413/415 semantic HTTP codes for size and format rejections
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_upload_unsupported_format_returns_415(async_client: AsyncClient):
    """Unknown extensions get 415 Unsupported Media Type with an `allowed`
    list in the structured detail so the frontend can render the hint
    without hard-coding the extension set."""
    response = await async_client.post(
        "/api/knowledge/upload",
        files=[("file", ("evil.exe", io.BytesIO(b"MZ..."), "application/octet-stream"))],
    )
    assert response.status_code == 415, response.text
    body = response.json()
    assert isinstance(body["detail"], dict)
    assert "allowed" in body["detail"]
    assert body["detail"]["received"] == "exe"


@pytest.mark.unit
async def test_upload_oversize_returns_413(async_client: AsyncClient, monkeypatch):
    """Files above `max_file_size_mb` get 413 Content Too Large with
    `max_mb` in the detail. We shrink the limit to 0 so even a tiny file
    trips it, keeping the test fast."""
    from utils.config import settings

    monkeypatch.setattr(settings, "max_file_size_mb", 0)
    response = await async_client.post(
        "/api/knowledge/upload",
        files=[("file", ("big.txt", io.BytesIO(b"x" * 2048), "text/plain"))],
    )
    assert response.status_code == 413, response.text
    body = response.json()
    assert body["detail"]["max_mb"] == 0
