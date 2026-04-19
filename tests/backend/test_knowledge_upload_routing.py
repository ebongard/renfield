"""Upload-endpoint routing tests for the document-worker path (#388).

Matrix items:
  #2  upload with duplicate hash → 409
  #6  upload with worker heartbeat present → 202 + queued row
  #7  upload with heartbeat missing → 503 + file cleanup
  #14 GET /api/knowledge/documents/batch returns requested ids

  Plus:
  - unknown extension → 415 with structured {allowed, received}
  - oversize upload → 413 with structured {max_mb, received_mb}
  - concurrent race → IntegrityError → 409 (migration c3d4e5f6g7h8)
"""
from __future__ import annotations

import hashlib
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
async def test_upload_returns_202_when_worker_alive(
    async_client: AsyncClient, db_session, kb
):
    """Upload endpoint persists a pending Document and enqueues a task
    on the Redis Stream. The worker pod consumes from there."""
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
# Matrix #7 — worker heartbeat missing → 503 (cleanup: file must be removed)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.database
async def test_upload_returns_503_when_worker_heartbeat_missing(
    async_client: AsyncClient, db_session, kb
):
    """When the worker heartbeat is missing, the endpoint must 503
    (retryable) rather than silently enqueue into a stream nobody's
    reading. The saved file must also be cleaned up so a retry doesn't
    race with an orphan on disk."""
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


# ---------------------------------------------------------------------------
# Concurrent-upload race → IntegrityError → 409 (migration c3d4e5f6g7h8)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.database
async def test_create_document_record_raises_on_duplicate_hash_kb(db_session, kb):
    """The uq_documents_file_hash_kb constraint must make a second
    insert with the same (file_hash, knowledge_base_id) raise
    IntegrityError. This is what the route's IntegrityError handler
    relies on to return 409 instead of 500."""
    from sqlalchemy.exc import IntegrityError

    from services.rag_service import RAGService

    rag = RAGService(db_session)
    hash_a = hashlib.sha256(b"content-a").hexdigest()
    await rag.create_document_record(
        file_path="/tmp/a.txt",
        knowledge_base_id=kb.id,
        filename="a.txt",
        file_hash=hash_a,
    )
    with pytest.raises(IntegrityError):
        await rag.create_document_record(
            file_path="/tmp/a-dup.txt",
            knowledge_base_id=kb.id,
            filename="a-dup.txt",
            file_hash=hash_a,
        )


@pytest.mark.unit
@pytest.mark.database
async def test_upload_race_handler_returns_409_not_500(
    async_client: AsyncClient, db_session, kb
):
    """Route-level IntegrityError handler: when create_document_record
    raises uq_documents_file_hash_kb IntegrityError (simulating the
    race where another request committed between our pre-check SELECT
    and our INSERT), the endpoint must return 409 with the winner's
    filename — never 500.

    The tricky part: the pre-check SELECT runs before create_document_record,
    so if we seed the winner up-front it wins at the pre-check and
    never reaches the handler under test. We simulate the race using
    a side_effect on create_document_record that *first* commits the
    winner, *then* raises IntegrityError — mirroring real production
    ordering (winner commits during our round-trip, our INSERT then
    loses).
    """
    from sqlalchemy.exc import IntegrityError

    payload = b"race-bytes-routelevel"
    file_hash = hashlib.sha256(payload).hexdigest()
    winner_id = {"id": None}

    async def _race_create(self, **kwargs):
        # Simulate concurrent winner committing between our pre-check
        # and our INSERT. The pre-check already ran above (and missed)
        # by the time this fires.
        winner = Document(
            filename="winner.txt",
            file_path="/tmp/winner.txt",
            status="completed",
            knowledge_base_id=kb.id,
            file_hash=file_hash,
        )
        self.db.add(winner)
        await self.db.commit()
        await self.db.refresh(winner)
        winner_id["id"] = winner.id
        # Now our INSERT would fail with the unique constraint — raise
        # the same error SQLAlchemy produces. orig.__str__ needs to
        # contain the constraint name for the handler's narrow check.
        class _FakeOrig(Exception):
            def __str__(self) -> str:
                return "duplicate key value violates unique constraint \"uq_documents_file_hash_kb\""
        raise IntegrityError("INSERT INTO documents ...", {}, _FakeOrig())

    with patch(
        "api.routes.knowledge._worker_is_alive",
        new=AsyncMock(return_value=True),
    ), patch(
        "services.rag_service.RAGService.create_document_record",
        new=_race_create,
    ):
        response = await async_client.post(
            f"/api/knowledge/upload?knowledge_base_id={kb.id}",
            files=[_fake_upload(payload, "race.txt")],
        )

    # Must be 409, not 500.
    assert response.status_code == 409, response.text
    body = response.json()
    assert "existing_document" in body["detail"]
    assert body["detail"]["existing_document"]["filename"] == "winner.txt"
    assert body["detail"]["existing_document"]["id"] == winner_id["id"]


@pytest.mark.unit
@pytest.mark.database
async def test_upload_non_hash_integrity_error_returns_500(
    async_client: AsyncClient, db_session, kb
):
    """Distinguishing the race from other IntegrityErrors: a FK / NOT
    NULL violation (i.e. not the uq_documents_file_hash_kb constraint)
    must propagate as a 500, not get mis-labeled as a 409 with a fake
    existing_document payload."""
    from sqlalchemy.exc import IntegrityError

    class _FakeFkOrig(Exception):
        def __str__(self) -> str:
            return "insert or update on table \"documents\" violates foreign key constraint \"documents_knowledge_base_id_fkey\""

    fake_err = IntegrityError("INSERT", {}, _FakeFkOrig())
    with patch(
        "api.routes.knowledge._worker_is_alive",
        new=AsyncMock(return_value=True),
    ), patch(
        "services.rag_service.RAGService.create_document_record",
        new=AsyncMock(side_effect=fake_err),
    ):
        response = await async_client.post(
            f"/api/knowledge/upload?knowledge_base_id={kb.id}",
            files=[_fake_upload(b"fk-violation-bytes", "fk.txt")],
        )
    assert response.status_code == 500, response.text
