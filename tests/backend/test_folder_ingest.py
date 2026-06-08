"""Tests for the folder-ingest feature (watch-folder auto-ingest).

Increment 1 covers the shared race-safe create helper (D3,
``RAGService.create_document_record_safe``) that both the upload route and
the folder-ingest bridge use, so the ``uq_documents_file_hash_kb``
concurrent-insert race is handled in exactly one place.

The classification logic (hash-race vs other IntegrityError) is unit-tested
with mocks: ``create_document_record`` commits internally, which is
incompatible with the rollback-isolated ``pg_db_session`` fixture, and the
behaviour under test is purely "given an IntegrityError, classify and react",
not the constraint itself. The constraint's real enforcement is covered
end-to-end by the route tests + the .159 E2E.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from services.rag_service import DuplicateDocumentError, RAGService


def _integrity_error(message: str) -> IntegrityError:
    """Build an IntegrityError whose ``str(orig)`` carries ``message`` —
    mirrors how asyncpg surfaces a unique-violation through SQLAlchemy."""
    return IntegrityError("INSERT INTO documents ...", {}, Exception(message))


def _bare_rag(create_side_effect=None, create_return=None) -> RAGService:
    """A RAGService with the heavy __init__ bypassed (no DocumentProcessor),
    wired with an AsyncMock db and a stubbed create_document_record."""
    rag = RAGService.__new__(RAGService)
    rag.db = AsyncMock()
    rag.create_document_record = AsyncMock(
        side_effect=create_side_effect, return_value=create_return
    )
    return rag


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_create_passthrough_returns_document():
    sentinel = object()
    rag = _bare_rag(create_return=sentinel)

    out = await rag.create_document_record_safe(
        file_path="/x.pdf", knowledge_base_id=1, filename="x.pdf", file_hash="h"
    )

    assert out is sentinel
    rag.db.rollback.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_create_hash_race_raises_duplicate_with_winner():
    rag = _bare_rag(
        create_side_effect=_integrity_error(
            'duplicate key value violates unique constraint "uq_documents_file_hash_kb"'
        )
    )
    winner = MagicMock(id=42)
    result = MagicMock()
    result.scalar_one_or_none.return_value = winner
    # Wire rollback + execute as children of ONE parent so mock_calls records
    # their relative ORDER: the rollback MUST precede the winner SELECT, else a
    # real asyncpg session is still in InFailedSqlTransaction and the SELECT
    # raises. The mocked db has no session state, so without this ordering check
    # a SELECT-before-rollback reorder would pass green.
    manager = MagicMock()
    manager.rollback = AsyncMock()
    manager.execute = AsyncMock(return_value=result)
    rag.db.rollback = manager.rollback
    rag.db.execute = manager.execute

    with pytest.raises(DuplicateDocumentError) as excinfo:
        await rag.create_document_record_safe(
            file_path="/x.pdf", knowledge_base_id=1, filename="x.pdf", file_hash="h"
        )

    assert excinfo.value.winner is winner
    rag.db.rollback.assert_awaited_once()
    ordered = [c[0] for c in manager.mock_calls]
    assert ordered.index("rollback") < ordered.index("execute"), (
        f"rollback must precede the winner SELECT; got order {ordered}"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_create_other_integrity_error_reraises():
    # A non-hash-race IntegrityError (FK / NOT NULL) must NOT be masked as a
    # duplicate — it re-raises so the caller can surface a genuine 500.
    rag = _bare_rag(
        create_side_effect=_integrity_error(
            'insert violates foreign key constraint "fk_documents_kb"'
        )
    )

    with pytest.raises(IntegrityError):
        await rag.create_document_record_safe(
            file_path="/x.pdf", knowledge_base_id=1, filename="x.pdf", file_hash="h"
        )

    rag.db.rollback.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_create_hash_race_winner_unfetchable_is_none():
    # If the winning row can't be re-fetched (already gone), the error still
    # raises with winner=None rather than blowing up.
    rag = _bare_rag(
        create_side_effect=_integrity_error(
            'duplicate key value violates unique constraint "uq_documents_file_hash_kb"'
        )
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    rag.db.execute = AsyncMock(return_value=result)

    with pytest.raises(DuplicateDocumentError) as excinfo:
        await rag.create_document_record_safe(
            file_path="/x.pdf", knowledge_base_id=1, filename="x.pdf", file_hash="h"
        )

    assert excinfo.value.winner is None
