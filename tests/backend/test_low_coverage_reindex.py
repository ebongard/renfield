"""Tests for the autonomous low-coverage reindex sweep + its scheduled built-in.

`sweep_low_coverage_reindex` re-enqueues completed docs whose LATEST processing
run dropped most of their content (low coverage) so the ingest-time VLM coverage
trigger recovers them. DB/queue/redis are mocked at their seams; the SQL itself is
compile-smoked against the Postgres dialect (`test_low_coverage_query_compiles`).
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.kb_maintenance_tool as kb

pytestmark = [pytest.mark.unit]


def _scalars_result(ids):
    r = MagicMock()
    r.scalars.return_value.all.return_value = ids
    return r


def _scalar_result(val):
    r = MagicMock()
    r.scalar.return_value = val
    return r


def _session(execute_results):
    session = MagicMock()
    session.execute = AsyncMock(side_effect=list(execute_results))
    session.commit = AsyncMock()

    @asynccontextmanager
    async def _cm():
        yield session

    return _cm, session


def _patch_queue(monkeypatch):
    q = MagicMock()
    q.enqueue = AsyncMock()
    monkeypatch.setattr("services.task_queue.DocumentTaskQueue", MagicMock(return_value=q))
    monkeypatch.setattr("services.redis_client.get_redis", MagicMock(return_value=MagicMock()))
    return q


# --------------------------------------------------------------- compile-smoke
def test_low_coverage_query_compiles():
    """The correlated EXISTS (drop-rate + latest-run NOT EXISTS + trigger) must
    compile against Postgres — mocked behavior tests never exercise the SQL."""
    from sqlalchemy import func, select
    from sqlalchemy.dialects import postgresql

    from models.database import DOC_STATUS_COMPLETED, Document

    for reindexable in (True, False):
        clause = kb._low_coverage_exists(0.7, reindexable=reindexable)
        count_q = (
            select(func.count()).select_from(Document).where(
                Document.status == DOC_STATUS_COMPLETED, clause
            )
        )
        col_q = select(Document.id).where(clause)
        for q in (count_q, col_q):
            sql = str(q.compile(dialect=postgresql.dialect())).lower()
            assert "document_processing_history" in sql
            assert "chunks_dropped_low_quality" in sql
            assert "not (exists" in sql  # the latest-completed-run guard


# --------------------------------------------------------------- sweep behavior
@pytest.mark.asyncio
async def test_sweep_enqueues_low_coverage(monkeypatch):
    # execute order: attempted-count, id-select (ctx 1), status-update (ctx 2).
    cm, session = _session([_scalar_result(2), _scalars_result([5, 9]), MagicMock()])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "ocr_vlm_coverage_drop_threshold", 0.7)
    q = _patch_queue(monkeypatch)

    report = await kb.sweep_low_coverage_reindex(cap=50)

    assert report == {"enqueued": 2, "skipped_attempted": 2}
    payloads = [c.args[0] for c in q.enqueue.await_args_list]
    assert {p["document_id"] for p in payloads} == {5, 9}
    # force_ocr MUST be False → the text-layer→VLM coverage path (force-OCR would
    # drop positioned tokens); trigger user_reindex.
    assert all(p["force_ocr"] is False for p in payloads)
    assert all(p["trigger"] == "user_reindex" for p in payloads)
    session.commit.assert_awaited()  # status flipped to pending


@pytest.mark.asyncio
async def test_sweep_empty_reports_skipped(monkeypatch):
    cm, _session_obj = _session([_scalar_result(3), _scalars_result([])])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    q = _patch_queue(monkeypatch)

    report = await kb.sweep_low_coverage_reindex(cap=50)

    assert report == {"enqueued": 0, "skipped_attempted": 3}
    q.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_enqueue_error_is_isolated(monkeypatch):
    """One bad enqueue must not abort the batch: doc 9 still enqueues."""
    cm, session = _session([_scalar_result(0), _scalars_result([5, 9]), MagicMock()])
    monkeypatch.setattr(kb, "AsyncSessionLocal", cm)
    monkeypatch.setattr(kb.settings, "ocr_vlm_coverage_drop_threshold", 0.7)

    q = MagicMock()

    async def _enqueue(payload):
        if payload["document_id"] == 5:
            raise RuntimeError("redis blip")

    q.enqueue = AsyncMock(side_effect=_enqueue)
    monkeypatch.setattr("services.task_queue.DocumentTaskQueue", MagicMock(return_value=q))
    monkeypatch.setattr("services.redis_client.get_redis", MagicMock(return_value=MagicMock()))

    report = await kb.sweep_low_coverage_reindex(cap=50)

    assert report == {"enqueued": 1, "skipped_attempted": 0}  # doc 5's error isolated
    session.commit.assert_awaited()  # the one success is still flipped to pending


@pytest.mark.asyncio
async def test_sweep_threshold_zero_is_noop(monkeypatch):
    q = _patch_queue(monkeypatch)
    report = await kb.sweep_low_coverage_reindex(cap=50, threshold=0.0)
    assert report == {"enqueued": 0, "skipped_attempted": 0}
    q.enqueue.assert_not_awaited()


# --------------------------------------------------------------- built-in handler
@pytest.mark.asyncio
async def test_handler_self_gates_when_disabled(monkeypatch):
    import services.scheduled_tasks.builtins as b

    monkeypatch.setattr(b.settings, "low_coverage_reindex_enabled", False)
    out = await b._low_coverage_reindex_handler(None, {})
    assert "skipped" in out and "low_coverage_reindex_enabled" in out


@pytest.mark.asyncio
async def test_handler_runs_sweep_when_enabled(monkeypatch):
    import services.scheduled_tasks.builtins as b

    monkeypatch.setattr(b.settings, "low_coverage_reindex_enabled", True)
    monkeypatch.setattr(b.settings, "low_coverage_reindex_cap", 50)
    fake = AsyncMock(return_value={"enqueued": 3, "skipped_attempted": 1})
    monkeypatch.setattr("services.kb_maintenance_tool.sweep_low_coverage_reindex", fake)

    out = await b._low_coverage_reindex_handler(None, {})

    assert "enqueued=3" in out and "skipped_attempted=1" in out
    fake.assert_awaited_once()
