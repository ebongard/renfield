"""Unit tests for the PDF-split slow lane (PR3): VLM page-signal fill-in,
the slow-lane job function, and the dedicated worker's claim/poison/terminal
semantics. Collaborators mocked — no pdfium, no LLM, no Redis, no DB engine.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import workers.pdf_split_worker as w

import services.pdf_split_detector as det
import services.pdf_split_slow_lane as lane
from models.database import (
    DOC_STATUS_PENDING,
    DOC_STATUS_SPLIT_ARCHIVED,
    DOC_STATUS_SPLIT_PENDING,
    DOC_STATUS_SPLIT_REVIEW,
)
from services.pdf_split_detector import PageSignal, SplitPiece, SplitVerdict
from services.pdf_split_errors import SplitExecutionError, SplitTransientError

pytestmark = [pytest.mark.unit]


def _sig(page, ok=True, text="Rechnung"):
    return PageSignal(page=page, text=text, quality_ok=ok)


def _piece(s, e, conf=0.9):
    return SplitPiece(start_page=s, end_page=e, title="T", doc_type="", confidence=conf)


# ---------------------------------------------------------------------------
# vlm_fill_signals
# ---------------------------------------------------------------------------

class TestVlmFillSignals:
    @pytest.mark.asyncio
    async def test_no_vision_model_returns_unchanged(self, monkeypatch):
        monkeypatch.setattr(det.settings, "ollama_vision_model", "")
        signals = [_sig(1), _sig(2, ok=False)]
        out, filled = await det.vlm_fill_signals("/x.pdf", signals)
        assert out == signals and filled == 0

    @pytest.mark.asyncio
    async def test_fills_only_garbage_pages_no_cap(self, monkeypatch):
        """EVERY garbage page is transcribed — deliberately no page cap (user
        requirement); clean pages are never re-transcribed."""
        monkeypatch.setattr(det.settings, "ollama_vision_model", "qwen-vl")
        monkeypatch.setattr(det.settings, "pdf_split_vlm_page_timeout_s", 5)
        monkeypatch.setattr(det, "_render_page_b64", MagicMock(return_value="b64"))
        svc = MagicMock()
        svc.extract_text_from_image = AsyncMock(
            side_effect=[f"Kopf Seite {i}" for i in range(50)]
        )
        signals = [_sig(p, ok=(p % 2 == 0)) for p in range(1, 21)]  # 10 garbage

        out, filled = await det.vlm_fill_signals(
            "/x.pdf", signals, ollama_service=svc
        )

        assert filled == 10
        assert svc.extract_text_from_image.await_count == 10
        assert all(s.quality_ok for s in out)
        assert sum(1 for s in out if s.via_vlm) == 10

    @pytest.mark.asyncio
    async def test_timeout_keeps_placeholder(self, monkeypatch):
        monkeypatch.setattr(det.settings, "ollama_vision_model", "qwen-vl")
        monkeypatch.setattr(det.settings, "pdf_split_vlm_page_timeout_s", 1)
        monkeypatch.setattr(det, "_render_page_b64", MagicMock(return_value="b64"))

        async def hang(_b64):
            await asyncio.sleep(30)

        svc = MagicMock()
        svc.extract_text_from_image = hang
        signals = [_sig(1, ok=False)]

        out, filled = await det.vlm_fill_signals("/x.pdf", signals, ollama_service=svc)

        assert filled == 0
        assert out[0].quality_ok is False  # placeholder kept, job continues

    @pytest.mark.asyncio
    async def test_render_or_vlm_failure_keeps_placeholder(self, monkeypatch):
        monkeypatch.setattr(det.settings, "ollama_vision_model", "qwen-vl")
        monkeypatch.setattr(det, "_render_page_b64", MagicMock(return_value=None))
        svc = MagicMock()
        svc.extract_text_from_image = AsyncMock()

        out, filled = await det.vlm_fill_signals(
            "/x.pdf", [_sig(1, ok=False)], ollama_service=svc
        )

        assert filled == 0
        svc.extract_text_from_image.assert_not_called()


# ---------------------------------------------------------------------------
# process_slow_split
# ---------------------------------------------------------------------------

class _FakeSessionCM:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_a):
        return False


def _doc(**over):
    defaults = dict(
        id=7,
        filename="stapel.pdf",
        file_path="/uploads/x_stapel.pdf",
        file_hash="h" * 64,
        knowledge_base_id=3,
        status=DOC_STATUS_SPLIT_PENDING,
        paperless_state=None,
        source=None,
        atom_id=None,
        circle_tier=0,
        error_message=None,
        chunk_count=0,
        split_from_document_id=None,
        split_heartbeat_at=None,
    )
    defaults.update(over)
    return SimpleNamespace(**defaults)


def _wire_lane(
    monkeypatch,
    *,
    doc,
    stored=None,
    rejected=False,
    signals=None,
    filled=0,
    verdict=None,
    outcome=None,
):
    db = MagicMock()
    db.commit = AsyncMock()
    db.get = AsyncMock(return_value=doc)
    monkeypatch.setattr(lane, "AsyncSessionLocal", lambda: _FakeSessionCM(db))
    plan_row = SimpleNamespace(id=1) if stored is not None else None
    monkeypatch.setattr(
        lane, "_load_stored_plan", AsyncMock(return_value=(plan_row, stored))
    )
    monkeypatch.setattr(lane, "_rejection_recorded", AsyncMock(return_value=rejected))
    monkeypatch.setattr(
        lane, "extract_page_signals", MagicMock(return_value=signals or [])
    )
    monkeypatch.setattr(
        lane, "vlm_fill_signals", AsyncMock(return_value=(signals or [], filled))
    )
    monkeypatch.setattr(
        lane,
        "detect_boundaries",
        AsyncMock(return_value=verdict or SplitVerdict(kind="single")),
    )
    act = AsyncMock(return_value=outcome or "single")
    monkeypatch.setattr(lane, "act_on_verdict", act)
    execute = AsyncMock(return_value=[101])
    monkeypatch.setattr(lane, "execute_split", execute)
    queue = MagicMock()
    queue.enqueue = AsyncMock()
    monkeypatch.setattr(lane, "DocumentTaskQueue", MagicMock(return_value=queue))
    monkeypatch.setattr(lane, "get_redis", MagicMock())
    return db, act, execute, queue


@pytest.mark.asyncio
async def test_slow_split_skips_resolved_docs(monkeypatch):
    for status in (DOC_STATUS_SPLIT_ARCHIVED, DOC_STATUS_SPLIT_REVIEW):
        db, act, execute, _ = _wire_lane(monkeypatch, doc=_doc(status=status))
        assert await lane.process_slow_split(7, None) == "skip"
        act.assert_not_called()
        execute.assert_not_called()


@pytest.mark.asyncio
async def test_slow_split_replays_stored_plan(monkeypatch):
    plan = [_piece(1, 2), _piece(3, 5)]
    db, act, execute, _ = _wire_lane(monkeypatch, doc=_doc(), stored=plan)

    assert await lane.process_slow_split(7, 5) == "split"

    execute.assert_awaited_once()
    assert execute.await_args.args[2] == plan
    act.assert_not_called()  # never re-detect over a persisted plan


@pytest.mark.asyncio
async def test_slow_split_honors_rejection(monkeypatch):
    doc = _doc()
    db, act, execute, queue = _wire_lane(monkeypatch, doc=doc, rejected=True)

    assert await lane.process_slow_split(7, 5) == "single"

    assert doc.status == DOC_STATUS_PENDING  # handed back
    params = queue.enqueue.await_args.args[0]
    assert params["skip_split"] is True
    act.assert_not_called()


@pytest.mark.asyncio
async def test_slow_split_no_signals_hands_back_single(monkeypatch):
    doc = _doc()
    db, act, execute, queue = _wire_lane(monkeypatch, doc=doc, signals=[])

    assert await lane.process_slow_split(7, None) == "single"

    assert doc.status == DOC_STATUS_PENDING
    assert queue.enqueue.await_args.args[0]["skip_split"] is True


@pytest.mark.asyncio
async def test_slow_split_single_verdict_hands_back(monkeypatch):
    doc = _doc()
    db, act, execute, queue = _wire_lane(
        monkeypatch, doc=doc, signals=[_sig(1), _sig(2)], outcome="single"
    )

    assert await lane.process_slow_split(7, None) == "single"

    assert doc.status == DOC_STATUS_PENDING
    assert queue.enqueue.await_args.args[0]["skip_split"] is True


@pytest.mark.asyncio
async def test_slow_split_confident_and_review_outcomes(monkeypatch):
    for outcome in ("split", "review"):
        doc = _doc()
        db, act, execute, queue = _wire_lane(
            monkeypatch, doc=doc, signals=[_sig(1), _sig(2)], outcome=outcome
        )
        assert await lane.process_slow_split(7, None) == outcome
        queue.enqueue.assert_not_called()  # no hand-back


# ---------------------------------------------------------------------------
# worker claim / poison / terminal semantics
# ---------------------------------------------------------------------------

def _wire_worker(monkeypatch, *, doc, process=None):
    db = MagicMock()
    db.commit = AsyncMock()
    db.get = AsyncMock(return_value=doc)
    monkeypatch.setattr(w, "AsyncSessionLocal", lambda: _FakeSessionCM(db))
    proc = AsyncMock(return_value=process or "split")
    monkeypatch.setattr(w, "process_slow_split", proc)
    hand_back = AsyncMock(return_value=True)
    monkeypatch.setattr(w, "_hand_back_as_single", hand_back)
    queue = MagicMock()
    queue.ack = AsyncMock()
    redis = MagicMock()
    redis.delete = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.incr = AsyncMock()
    redis.expire = AsyncMock()
    return db, proc, hand_back, queue, redis


def _entry(doc_id=7, delivery_count=1):
    return SimpleNamespace(
        entry_id="1-0", params={"document_id": doc_id, "user_id": None},
        delivery_count=delivery_count,
    )


@pytest.mark.asyncio
async def test_worker_processes_claimed_row(monkeypatch):
    doc = _doc()
    _, proc, hand_back, queue, redis = _wire_worker(monkeypatch, doc=doc)

    await w._process_entry(redis, queue, _entry())

    proc.assert_awaited_once()
    queue.ack.assert_awaited_once_with("1-0")
    hand_back.assert_not_called()


@pytest.mark.asyncio
async def test_worker_skips_resolved_doc(monkeypatch):
    doc = _doc(status=DOC_STATUS_SPLIT_ARCHIVED)
    _, proc, _, queue, redis = _wire_worker(monkeypatch, doc=doc)

    await w._process_entry(redis, queue, _entry())

    proc.assert_not_called()
    queue.ack.assert_awaited_once_with("1-0")


@pytest.mark.asyncio
async def test_worker_waits_on_live_job(monkeypatch):
    from datetime import datetime

    doc = _doc(split_heartbeat_at=datetime.utcnow())
    _, proc, _, queue, redis = _wire_worker(monkeypatch, doc=doc)

    await w._process_entry(redis, queue, _entry())

    proc.assert_not_called()
    queue.ack.assert_not_called()  # left in PEL


@pytest.mark.asyncio
async def test_worker_poison_hands_back_single_never_failed(monkeypatch):
    """The poison outcome is the single-ingest hand-back — a slow-lane doc is
    processable, only the split decision kept dying."""
    doc = _doc()
    _, proc, hand_back, queue, redis = _wire_worker(monkeypatch, doc=doc)
    monkeypatch.setattr(w.settings, "worker_max_deliveries", 3)

    await w._process_entry(redis, queue, _entry(delivery_count=5))

    hand_back.assert_awaited_once()
    proc.assert_not_called()
    queue.ack.assert_awaited_once_with("1-0")


@pytest.mark.asyncio
async def test_worker_transient_cap_hands_back_single(monkeypatch):
    doc = _doc()
    _, proc, hand_back, queue, redis = _wire_worker(monkeypatch, doc=doc)
    monkeypatch.setattr(w.settings, "pdf_split_worker_max_transient_retries", 2)
    redis.get = AsyncMock(return_value="5")  # transient leaves

    await w._process_entry(redis, queue, _entry(delivery_count=6))

    hand_back.assert_awaited_once()
    proc.assert_not_called()


@pytest.mark.asyncio
async def test_worker_transient_error_stays_in_pel(monkeypatch):
    doc = _doc()
    _, proc, hand_back, queue, redis = _wire_worker(monkeypatch, doc=doc)
    proc.side_effect = SplitTransientError("ollama down")

    await w._process_entry(redis, queue, _entry())

    queue.ack.assert_not_called()
    redis.incr.assert_awaited_once()  # clean transient leave
    hand_back.assert_not_called()


@pytest.mark.asyncio
async def test_worker_terminal_execution_error_marks_failed(monkeypatch):
    """A terminal SplitExecutionError mid-execute means children may already
    exist — a single-ingest hand-back would double-ingest, so THIS case marks
    the doc failed (re-push REINGEST is the retry)."""
    doc = _doc()
    _, proc, hand_back, queue, redis = _wire_worker(monkeypatch, doc=doc)
    proc.side_effect = SplitExecutionError("part 2 failed terminally")

    await w._process_entry(redis, queue, _entry())

    assert doc.status == "failed"
    hand_back.assert_not_called()
    queue.ack.assert_awaited_once_with("1-0")
