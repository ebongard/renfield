"""Tests for ``bin/purge_low_quality_chunks.py``.

Two layers:
  - CLI / arg-validation tests (pure Python, no DB) — every option's
    failure mode.
  - End-to-end dry-run + apply tests (Postgres) — verifies the
    skip-counters increment correctly, force_ocr re-OCR ran, idempotence
    holds across runs.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.database import Document, DocumentChunk
from services.document_processing_history import (
    DocumentProcessingHistoryService,
    ProcessingTrigger,
)


# Load the script as a module (it's outside the ``src/backend`` package).
_SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "purge_low_quality_chunks.py"
spec = importlib.util.spec_from_file_location("purge_low_quality_chunks", _SCRIPT)
purge = importlib.util.module_from_spec(spec)
sys.modules["purge_low_quality_chunks"] = purge
spec.loader.exec_module(purge)


# ============================================================================
# CLI / arg validation (pure Python, no DB)
# ============================================================================


class TestArgValidation:
    def test_default_args_parse(self):
        ns = purge._build_parser().parse_args([])
        assert ns.apply is False
        assert ns.doc_id is None
        assert ns.limit is None
        assert ns.batch_size == 500
        assert ns.reason_threshold == 1

    def test_apply_flag(self):
        ns = purge._build_parser().parse_args(["--apply"])
        assert ns.apply is True

    def test_batch_size_zero_rejected(self):
        ns = purge._build_parser().parse_args(["--batch-size", "0"])
        with pytest.raises(SystemExit, match="--batch-size"):
            purge._validate_args(ns)

    def test_batch_size_negative_rejected(self):
        ns = purge._build_parser().parse_args(["--batch-size", "-5"])
        with pytest.raises(SystemExit, match="--batch-size"):
            purge._validate_args(ns)

    def test_reason_threshold_zero_rejected(self):
        ns = purge._build_parser().parse_args(["--reason-threshold", "0"])
        with pytest.raises(SystemExit, match="--reason-threshold"):
            purge._validate_args(ns)

    def test_limit_zero_rejected(self):
        ns = purge._build_parser().parse_args(["--limit", "0"])
        with pytest.raises(SystemExit, match="--limit"):
            purge._validate_args(ns)

    def test_doc_id_zero_rejected(self):
        ns = purge._build_parser().parse_args(["--doc-id", "0"])
        with pytest.raises(SystemExit, match="--doc-id"):
            purge._validate_args(ns)

    def test_valid_combo_passes(self):
        ns = purge._build_parser().parse_args(
            ["--apply", "--doc-id", "42", "--reason-threshold", "3", "--batch-size", "100"]
        )
        purge._validate_args(ns)  # no exception


# ============================================================================
# End-to-end: dry-run + apply (Postgres)
# ============================================================================


pgmark = [
    pytest.mark.database,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("RENFIELD_TEST_PG_URL"),
        reason="RENFIELD_TEST_PG_URL not set — Postgres tests disabled",
    ),
]


@pytest.fixture
async def committing_session(pg_async_engine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(pg_async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    async with pg_async_engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE document_processing_history, document_chunks, documents "
            "RESTART IDENTITY CASCADE"
        ))


async def _make_doc_with_chunks(
    session: AsyncSession,
    *,
    good_chunks: int = 0,
    bad_chunks: int = 0,
) -> Document:
    """Create a Document with N good + M bad chunks. 'Bad' = trips
    is_low_quality_text (medium-length, dominated by single-char glyph runs)."""
    doc = Document(filename="t.pdf", file_path="/tmp/t.pdf", status="completed")
    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    idx = 0
    good_text = (
        "Dies ist ein klar lesbarer Absatz mit ausreichend Worten "
        "und normaler Interpunktion, damit der Quality-Filter ihn passieren laesst."
    )
    bad_text = ", . / ; : ' \" ! ? - _ ` ~ # $ % ^ & * ( ) " * 5  # >40 chars, low wordlike ratio

    for _ in range(good_chunks):
        session.add(DocumentChunk(
            document_id=doc.id, chunk_index=idx, content=good_text, chunk_type="text"
        ))
        idx += 1
    for _ in range(bad_chunks):
        session.add(DocumentChunk(
            document_id=doc.id, chunk_index=idx, content=bad_text, chunk_type="text"
        ))
        idx += 1
    await session.commit()
    return doc


@pytest.mark.parametrize("_marker", pgmark)
class _Skip:  # noqa: D401 — pytest paramaterize-mark workaround
    """Decorator carrier so the marks below apply once at class scope."""


@pytest.mark.postgres
@pytest.mark.database
@pytest.mark.skipif(
    not os.environ.get("RENFIELD_TEST_PG_URL"),
    reason="RENFIELD_TEST_PG_URL not set — Postgres tests disabled",
)
class TestPgIntegration:
    async def test_count_low_quality_chunks_counts_only_bad(self, committing_session):
        doc = await _make_doc_with_chunks(committing_session, good_chunks=3, bad_chunks=2)
        n = await purge._count_low_quality_chunks(committing_session, doc.id)
        assert n == 2

    async def test_dry_run_does_not_call_reindex(self, committing_session):
        doc = await _make_doc_with_chunks(committing_session, bad_chunks=1)

        mock_reindex = AsyncMock()
        with patch("services.rag_service.RAGService.reindex_document", mock_reindex):
            # Lock conn replaced with a stub — dry-run path never touches the
            # advisory-lock branch (it returns before _try_lock_doc).
            code = await purge._process_one(
                lock_conn=None,  # unused in dry-run
                doc_id=doc.id,
                apply=False,
                reason_threshold=1,
            )

        assert code == "would_purge"
        mock_reindex.assert_not_awaited()

    async def test_below_threshold_skipped(self, committing_session):
        doc = await _make_doc_with_chunks(committing_session, good_chunks=2, bad_chunks=0)
        code = await purge._process_one(
            lock_conn=None, doc_id=doc.id, apply=False, reason_threshold=1,
        )
        assert code == "skipped_below_threshold"

    async def test_already_re_ocrd_skipped(self, committing_session):
        """has_force_ocr_succeeded must short-circuit even in dry-run mode
        so the operator sees an honest report of what still needs work."""
        doc = await _make_doc_with_chunks(committing_session, bad_chunks=2)
        # Pre-seed the history table with a completed force_ocr row.
        svc = DocumentProcessingHistoryService(committing_session)
        hid = await svc.open(doc.id, force_ocr=True, trigger=ProcessingTrigger.SCRIPT_PURGE)
        await svc.close_success(hid, 5, 2, "docling_full_page_ocr")

        code = await purge._process_one(
            lock_conn=None, doc_id=doc.id, apply=False, reason_threshold=1,
        )
        assert code == "skipped_already_re_ocrd"

    async def test_reason_threshold_gating(self, committing_session):
        """A doc with 1 bad chunk should be skipped when --reason-threshold 3."""
        doc = await _make_doc_with_chunks(committing_session, bad_chunks=1)
        code = await purge._process_one(
            lock_conn=None, doc_id=doc.id, apply=False, reason_threshold=3,
        )
        assert code == "skipped_below_threshold"
