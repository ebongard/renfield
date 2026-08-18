"""Unit tests for PDF-split execution + the document-worker pre-stage seam.

``execute_split`` collaborators (db, ingest bridge, pdfium) are mocked; the
pdfium roundtrip itself is covered where pypdfium2 is installed (the worker
image has it via Docling — importorskip locally). ``maybe_split_at_ingest``
branch coverage asserts the hard invariants: flag-off is a no-op, split
children never re-enter detection, a persisted plan replays verbatim on
resume (never re-detect), detection errors never break ingest EXCEPT transient
LLM failures (PEL retry), and a partially-executed split is never followed by
normal ingest of the combined parent.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.pdf_splitter as ps
from models.database import (
    DOC_STATUS_PENDING,
    DOC_STATUS_SPLIT_ARCHIVED,
    DOC_STATUS_SPLIT_PENDING,
    DOC_STATUS_SPLIT_REVIEW,
    PAPERLESS_STATE_DONE,
    PAPERLESS_STATE_PENDING,
)
from services.folder_ingest import IngestResult, IngestStatus
from services.pdf_split_detector import (
    VERDICT_MULTI,
    VERDICT_SINGLE,
    PageSignal,
    SplitPiece,
    SplitVerdict,
)
from services.pdf_splitter import (
    SplitExecutionError,
    SplitTransientError,
    child_filename,
    execute_split,
    maybe_split_at_ingest,
)

# asyncio tests run via asyncio_mode=auto (pyproject / the .159 -o flag);
# no module-wide asyncio mark so the sync TestChildFilename cases stay clean.
pytestmark = [pytest.mark.unit]

_HASH = "a1b2c3d4e5f6a7b8" + "0" * 48


def _piece(s, e, title="Rechnung Stadtwerke", conf=0.9):
    return SplitPiece(start_page=s, end_page=e, title=title, doc_type="invoice", confidence=conf)


def _parent(**over):
    defaults = dict(
        id=7,
        filename="stapel_scan.pdf",
        file_path="/uploads/abc_stapel_scan.pdf",
        file_hash=_HASH,
        knowledge_base_id=3,
        status=DOC_STATUS_PENDING,
        paperless_state=PAPERLESS_STATE_PENDING,
        source=None,
        atom_id=None,
        circle_tier=0,
        error_message=None,
        chunk_count=0,
        split_from_document_id=None,
    )
    defaults.update(over)
    return SimpleNamespace(**defaults)


def _db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()
    db.get = AsyncMock(return_value=None)
    db.add = MagicMock()
    return db


# ---------------------------------------------------------------------------
# child_filename — the deterministic, parent-scoped resume key
# ---------------------------------------------------------------------------

class TestChildFilename:
    def test_deterministic(self):
        a = child_filename("Stapel Scan 2026.pdf", _HASH, 3, "Rechnung Müller GmbH")
        b = child_filename("Stapel Scan 2026.pdf", _HASH, 3, "Rechnung Müller GmbH")
        assert a == b

    def test_shape_umlauts_and_hash_token(self):
        name = child_filename("Stapel Scan.pdf", _HASH, 1, "Rechnung Müller & Söhne")
        assert name == "stapel-scan_a1b2c3d4_teil01_rechnung-mueller-soehne.pdf"

    def test_different_parent_hash_never_collides(self):
        # Same scanner filename + same recurring title slug, different content:
        # the hash token keeps the resume keys distinct across batch scans.
        a = child_filename("scan.pdf", "a" * 64, 1, "Rechnung Stadtwerke")
        b = child_filename("scan.pdf", "b" * 64, 1, "Rechnung Stadtwerke")
        assert a != b

    def test_empty_title_and_missing_hash_still_valid(self):
        assert child_filename("x.pdf", None, 2, "") == "x_nohash_teil02_dokument.pdf"


# ---------------------------------------------------------------------------
# execute_split
# ---------------------------------------------------------------------------

def _wire_split(monkeypatch, *, existing=None, ingest_results=None, owner=11):
    """Patch execute_split collaborators. ``existing`` maps part index (0-based)
    → fake existing child row. ``ingest_results`` is consumed in order for the
    missing parts."""
    existing = existing or {}
    calls = {"ingest": [], "split_calls": []}

    async def fake_existing_children(db, parent, names):
        by_name = {}
        for i, row in existing.items():
            for name in names:
                if f"teil{i + 1:02d}" in name:
                    by_name[name] = row
        return by_name

    async def fake_ingest(data, meta, **kwargs):
        calls["ingest"].append((data, meta, kwargs))
        return ingest_results.pop(0)

    def fake_split(file_path, pieces):
        calls["split_calls"].append(list(pieces))
        return [b"%PDF-part%" for _ in pieces]

    monkeypatch.setattr(ps, "_existing_children", fake_existing_children)
    monkeypatch.setattr(ps, "ingest_document", fake_ingest)
    monkeypatch.setattr(ps, "split_pdf_bytes", fake_split)
    monkeypatch.setattr(
        ps, "_resolve_parent_owner", AsyncMock(return_value=owner)
    )
    return calls


async def test_execute_split_creates_all_parts_and_archives_last(monkeypatch):
    db = _db()
    parent = _parent()
    child = SimpleNamespace(id=101, split_from_document_id=None)
    db.get = AsyncMock(return_value=child)
    calls = _wire_split(
        monkeypatch,
        ingest_results=[
            IngestResult(IngestStatus.INGESTED, document_id=101),
            IngestResult(IngestStatus.INGESTED, document_id=102),
        ],
    )

    out = await execute_split(db, parent, [_piece(1, 2), _piece(3, 5)])

    assert out == [101, 102]
    assert len(calls["ingest"]) == 2
    # one render call PER piece (no all-parts-in-RAM batch)
    assert [len(c) for c in calls["split_calls"]] == [1, 1]
    # children inherit kb / owner / tier / paperless intent / lineage source
    _, meta, kwargs = calls["ingest"][0]
    assert kwargs["kb_id"] == 3
    assert kwargs["owner_user_id"] == 11
    assert kwargs["file_to_paperless"] is True
    assert kwargs["source"] == "pdf_split"
    assert meta.filename.endswith(".pdf")
    # parent archived + Paperless settled
    assert parent.status == DOC_STATUS_SPLIT_ARCHIVED
    assert parent.paperless_state == PAPERLESS_STATE_DONE


async def test_execute_split_resume_skips_existing_parts(monkeypatch):
    db = _db()
    parent = _parent()
    part1 = SimpleNamespace(id=201, split_from_document_id=7)
    child2 = SimpleNamespace(id=202, split_from_document_id=None)
    db.get = AsyncMock(return_value=child2)
    calls = _wire_split(
        monkeypatch,
        existing={0: part1},
        ingest_results=[IngestResult(IngestStatus.INGESTED, document_id=202)],
    )

    out = await execute_split(db, parent, [_piece(1, 2), _piece(3, 5)])

    assert out == [201, 202]
    assert len(calls["ingest"]) == 1  # only the missing part
    assert [
        (p.start_page, p.end_page) for c in calls["split_calls"] for p in c
    ] == [(3, 5)]  # only the missing piece was rendered
    assert parent.status == DOC_STATUS_SPLIT_ARCHIVED


async def test_execute_split_resume_stamps_unstamped_existing_child(monkeypatch):
    # Crash window: child created but split_from not stamped — resume stamps it.
    db = _db()
    parent = _parent()
    part1 = SimpleNamespace(id=201, split_from_document_id=None)
    child2 = SimpleNamespace(id=202, split_from_document_id=None)
    db.get = AsyncMock(return_value=child2)
    _wire_split(
        monkeypatch,
        existing={0: part1},
        ingest_results=[IngestResult(IngestStatus.INGESTED, document_id=202)],
    )

    await execute_split(db, parent, [_piece(1, 2), _piece(3, 5)])

    assert part1.split_from_document_id == 7


async def test_execute_split_failed_part_raises_terminal_and_leaves_parent(monkeypatch):
    db = _db()
    parent = _parent()
    _wire_split(
        monkeypatch,
        ingest_results=[
            IngestResult(IngestStatus.INGESTED, document_id=101),
            IngestResult(IngestStatus.FAILED, detail="create_error"),
        ],
    )
    db.get = AsyncMock(return_value=SimpleNamespace(id=101, split_from_document_id=None))

    with pytest.raises(SplitExecutionError) as exc:
        await execute_split(db, parent, [_piece(1, 2), _piece(3, 5)])

    assert not isinstance(exc.value, SplitTransientError)  # genuinely terminal
    # NOT archived — parked mid-split (the flag-independent worker guard
    # protects this state against normal ingest)
    assert parent.status == DOC_STATUS_SPLIT_PENDING


async def test_execute_split_retry_part_raises_transient(monkeypatch):
    """A RETRY-class child outcome (disk full, lost create race) must be
    TRANSIENT — the worker leaves the entry in the PEL and the idempotent
    resume continues later, instead of permanently failing the parent."""
    db = _db()
    parent = _parent()
    _wire_split(
        monkeypatch,
        ingest_results=[IngestResult(IngestStatus.RETRY, detail="persist_error")],
    )

    with pytest.raises(SplitTransientError):
        await execute_split(db, parent, [_piece(1, 2), _piece(3, 5)])

    # NOT archived, NOT failed — parked mid-split for the PEL retry
    assert parent.status == DOC_STATUS_SPLIT_PENDING


async def test_execute_split_duplicate_part_counts_as_covered(monkeypatch):
    db = _db()
    parent = _parent()
    _wire_split(
        monkeypatch,
        ingest_results=[
            IngestResult(IngestStatus.DUPLICATE, document_id=55),
            IngestResult(IngestStatus.INGESTED, document_id=102),
        ],
    )
    db.get = AsyncMock(return_value=SimpleNamespace(id=102, split_from_document_id=None))

    out = await execute_split(db, parent, [_piece(1, 2), _piece(3, 5)])

    assert out == [55, 102]
    assert parent.status == DOC_STATUS_SPLIT_ARCHIVED


async def test_execute_split_already_archived_is_noop(monkeypatch):
    db = _db()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [101, 102]
    db.execute = AsyncMock(return_value=result)
    parent = _parent(status=DOC_STATUS_SPLIT_ARCHIVED)
    calls = _wire_split(monkeypatch, ingest_results=[])

    out = await execute_split(db, parent, [_piece(1, 2), _piece(3, 5)])

    assert out == [101, 102]
    assert calls["ingest"] == []


async def test_execute_split_refuses_single_piece(monkeypatch):
    _wire_split(monkeypatch, ingest_results=[])
    with pytest.raises(SplitExecutionError):
        await execute_split(_db(), _parent(), [_piece(1, 5)])


async def test_execute_split_no_paperless_parent_means_no_paperless_children(monkeypatch):
    db = _db()
    parent = _parent(paperless_state=None)
    db.get = AsyncMock(return_value=SimpleNamespace(id=1, split_from_document_id=None))
    calls = _wire_split(
        monkeypatch,
        ingest_results=[
            IngestResult(IngestStatus.INGESTED, document_id=1),
            IngestResult(IngestStatus.INGESTED, document_id=2),
        ],
    )

    await execute_split(db, parent, [_piece(1, 1), _piece(2, 2)])

    assert all(c[2]["file_to_paperless"] is False for c in calls["ingest"])


# ---------------------------------------------------------------------------
# split_pdf_bytes — real pdfium roundtrip (runs where pypdfium2 is installed)
# ---------------------------------------------------------------------------

async def test_split_pdf_bytes_roundtrip(tmp_path):
    pdfium = pytest.importorskip("pypdfium2")

    src = pdfium.PdfDocument.new()
    for _ in range(6):
        src.new_page(595, 842)
    src_path = tmp_path / "combined.pdf"
    src.save(str(src_path))
    src.close()

    parts = ps.split_pdf_bytes(
        str(src_path), [_piece(1, 2), _piece(3, 3), _piece(4, 6)]
    )

    assert len(parts) == 3
    for data, expected_pages in zip(parts, (2, 1, 3), strict=True):
        out = pdfium.PdfDocument(data)
        try:
            assert len(out) == expected_pages
        finally:
            out.close()


# ---------------------------------------------------------------------------
# maybe_split_at_ingest — the worker pre-stage seam
# ---------------------------------------------------------------------------

def _sig(page):
    return PageSignal(page=page, text="Rechnung", quality_ok=True)


def _wire_prestage(
    monkeypatch,
    *,
    enabled=True,
    doc=None,
    signals=None,
    slow=None,
    verdict=None,
    stored_plan=None,
    threshold=0.85,
):
    monkeypatch.setattr(ps.settings, "pdf_split_enabled", enabled)
    monkeypatch.setattr(ps.settings, "pdf_split_auto_threshold", threshold)
    db = _db()
    db.get = AsyncMock(return_value=doc)
    plan_row = SimpleNamespace(id=1) if stored_plan is not None else None
    monkeypatch.setattr(
        ps, "_load_stored_plan", AsyncMock(return_value=(plan_row, stored_plan))
    )
    store_row = SimpleNamespace(id=2)
    store = AsyncMock(return_value=store_row)
    monkeypatch.setattr(ps, "_store_plan", store)
    monkeypatch.setattr(
        ps, "extract_page_signals", MagicMock(return_value=signals or [])
    )
    monkeypatch.setattr(ps, "classify_slow_lane", MagicMock(return_value=slow))
    monkeypatch.setattr(
        ps,
        "detect_boundaries",
        AsyncMock(return_value=verdict or SplitVerdict(kind=VERDICT_SINGLE)),
    )
    execute = AsyncMock(return_value=[101, 102])
    monkeypatch.setattr(ps, "execute_split", execute)
    return db, execute, store


async def test_prestage_flag_off_is_noop(monkeypatch):
    db, execute, _ = _wire_prestage(monkeypatch, enabled=False, doc=_parent())
    assert await maybe_split_at_ingest(db, 7) is False
    db.get.assert_not_called()
    execute.assert_not_called()


async def test_prestage_skip_split_param_is_loop_breaker(monkeypatch):
    db, execute, _ = _wire_prestage(monkeypatch, doc=_parent())
    assert await maybe_split_at_ingest(db, 7, skip_split=True) is False
    db.get.assert_not_called()
    execute.assert_not_called()


async def test_prestage_split_child_never_re_enters_detection(monkeypatch):
    """A split child (lineage set) must NOT re-run detection — that would
    waste a boundary-LLM call per child and risk recursive re-splitting."""
    doc = _parent(split_from_document_id=42)
    db, execute, _ = _wire_prestage(monkeypatch, doc=doc)
    detector = MagicMock()
    monkeypatch.setattr(ps, "extract_page_signals", detector)

    assert await maybe_split_at_ingest(db, 7) is False

    detector.assert_not_called()
    execute.assert_not_called()


async def test_prestage_non_pdf_is_noop(monkeypatch):
    doc = _parent(filename="notes.docx", file_path="/uploads/x_notes.docx")
    db, execute, _ = _wire_prestage(monkeypatch, doc=doc)
    assert await maybe_split_at_ingest(db, 7) is False
    execute.assert_not_called()


async def test_prestage_missing_doc_is_noop(monkeypatch):
    db, _, _ = _wire_prestage(monkeypatch, doc=None)
    assert await maybe_split_at_ingest(db, 7) is False


@pytest.mark.parametrize(
    "status", [DOC_STATUS_SPLIT_ARCHIVED, DOC_STATUS_SPLIT_REVIEW]
)
async def test_prestage_parked_states_are_acked(monkeypatch, status):
    """A redelivered entry for a PARKED doc (archived / awaiting review) →
    True (ack, skip normal processing) WITHOUT re-running detection."""
    db, execute, _ = _wire_prestage(monkeypatch, doc=_parent(status=status))
    detector = MagicMock()
    monkeypatch.setattr(ps, "extract_page_signals", detector)

    assert await maybe_split_at_ingest(db, 7) is True

    detector.assert_not_called()
    execute.assert_not_called()


async def test_prestage_stored_plan_replays_without_detection(monkeypatch):
    """Crash-resume determinism: a persisted plan is replayed verbatim — the
    nondeterministic boundary LLM must NOT run again (its drift could silently
    drop pages from the resume-keyed parts)."""
    plan = [_piece(1, 2), _piece(3, 5)]
    doc = _parent(status=DOC_STATUS_SPLIT_PENDING)
    db, execute, store = _wire_prestage(monkeypatch, doc=doc, stored_plan=plan)
    detector = MagicMock()
    monkeypatch.setattr(ps, "extract_page_signals", detector)

    assert await maybe_split_at_ingest(db, 7, user_id=5) is True

    detector.assert_not_called()  # no re-detection
    store.assert_not_called()  # no second plan row
    execute.assert_awaited_once()
    assert execute.await_args.args[2] == plan
    assert execute.await_args.kwargs["plan_row"] is not None


async def test_prestage_mid_split_without_plan_unparks_and_redetects(monkeypatch):
    """split_pending with NO usable stored plan (corrupt / lost) must not
    strand the doc: it is un-parked back to pending and re-detected loudly."""
    doc = _parent(status=DOC_STATUS_SPLIT_PENDING)
    db, execute, _ = _wire_prestage(
        monkeypatch,
        doc=doc,
        signals=[_sig(1), _sig(2)],
        verdict=SplitVerdict(kind=VERDICT_SINGLE),
    )

    assert await maybe_split_at_ingest(db, 7) is False  # single verdict now

    assert doc.status == DOC_STATUS_PENDING  # un-parked
    execute.assert_not_called()


async def test_prestage_detection_error_falls_through(monkeypatch):
    db, execute, _ = _wire_prestage(monkeypatch, doc=_parent())
    monkeypatch.setattr(
        ps, "extract_page_signals", MagicMock(side_effect=RuntimeError("pdfium"))
    )
    assert await maybe_split_at_ingest(db, 7) is False
    execute.assert_not_called()


async def test_prestage_transient_llm_error_propagates(monkeypatch):
    """A transient LLM failure must NOT silently commit a single-doc verdict —
    it propagates so the worker PEL-retries the entry."""
    db, execute, _ = _wire_prestage(
        monkeypatch, doc=_parent(), signals=[_sig(1), _sig(2)]
    )
    monkeypatch.setattr(
        ps,
        "detect_boundaries",
        AsyncMock(side_effect=SplitTransientError("ollama down")),
    )

    with pytest.raises(SplitTransientError):
        await maybe_split_at_ingest(db, 7)

    execute.assert_not_called()


async def test_prestage_plan_lookup_db_error_propagates(monkeypatch):
    """A DB error during the stored-plan SELECT is transient infrastructure —
    it must propagate for a worker PEL retry, NOT degrade into fresh
    (nondeterministic) re-detection."""
    from sqlalchemy.exc import OperationalError

    db, execute, _ = _wire_prestage(monkeypatch, doc=_parent())
    monkeypatch.setattr(
        ps,
        "_load_stored_plan",
        AsyncMock(side_effect=OperationalError("SELECT", {}, Exception("blip"))),
    )

    with pytest.raises(OperationalError):
        await maybe_split_at_ingest(db, 7)

    execute.assert_not_called()


async def test_prestage_no_signals_falls_through(monkeypatch):
    db, execute, _ = _wire_prestage(monkeypatch, doc=_parent(), signals=[])
    assert await maybe_split_at_ingest(db, 7) is False
    execute.assert_not_called()


async def test_prestage_slow_lane_is_status_quo_in_pr1(monkeypatch):
    db, execute, _ = _wire_prestage(
        monkeypatch, doc=_parent(), signals=[_sig(1), _sig(2)], slow="vlm"
    )
    assert await maybe_split_at_ingest(db, 7) is False
    execute.assert_not_called()


async def test_prestage_single_verdict_falls_through(monkeypatch):
    db, execute, _ = _wire_prestage(
        monkeypatch,
        doc=_parent(),
        signals=[_sig(1), _sig(2)],
        verdict=SplitVerdict(kind=VERDICT_SINGLE),
    )
    assert await maybe_split_at_ingest(db, 7) is False
    execute.assert_not_called()


async def test_prestage_low_confidence_is_status_quo_in_pr1(monkeypatch):
    verdict = SplitVerdict(
        kind=VERDICT_MULTI, pieces=[_piece(1, 1, conf=0.95), _piece(2, 2, conf=0.5)]
    )
    db, execute, _ = _wire_prestage(
        monkeypatch, doc=_parent(), signals=[_sig(1), _sig(2)], verdict=verdict
    )
    assert await maybe_split_at_ingest(db, 7) is False
    execute.assert_not_called()


async def test_prestage_confident_multi_stores_plan_then_executes(monkeypatch):
    verdict = SplitVerdict(
        kind=VERDICT_MULTI,
        pieces=[_piece(1, 1), _piece(2, 2)],
        page_signals=[_sig(1), _sig(2)],
    )
    doc = _parent()
    db, execute, store = _wire_prestage(
        monkeypatch, doc=doc, signals=[_sig(1), _sig(2)], verdict=verdict
    )

    assert await maybe_split_at_ingest(db, 7, user_id=5) is True

    store.assert_awaited_once()  # plan persisted BEFORE execution
    execute.assert_awaited_once()
    args, kwargs = execute.await_args
    assert args[1] is doc
    assert args[2] == verdict.pieces
    assert kwargs["user_id"] == 5
    assert kwargs["plan_row"] is store.return_value  # resolutions recorded on it


async def test_prestage_execution_error_propagates(monkeypatch):
    """A failure while EXECUTING the split must NOT degrade to normal ingest
    (children may already exist — the combined parent would double-ingest)."""
    verdict = SplitVerdict(kind=VERDICT_MULTI, pieces=[_piece(1, 1), _piece(2, 2)])
    db, execute, _ = _wire_prestage(
        monkeypatch, doc=_parent(), signals=[_sig(1), _sig(2)], verdict=verdict
    )
    execute.side_effect = SplitExecutionError("part 2 failed")

    with pytest.raises(SplitExecutionError):
        await maybe_split_at_ingest(db, 7)


# ---------------------------------------------------------------------------
# stored-plan loading (validation against the real validate_boundaries)
# ---------------------------------------------------------------------------

async def test_load_stored_plan_validates_against_row_page_count(monkeypatch):
    """Revalidation uses the ROW's persisted page_count — never a live pdfium
    probe whose transient failure would discard a valid plan and re-open the
    nondeterministic detection path."""
    row = SimpleNamespace(
        page_count=5,
        proposal=[
            {"start_page": 1, "end_page": 2, "title": "A", "doc_type": "", "confidence": 0.9},
            {"start_page": 3, "end_page": 5, "title": "B", "doc_type": "", "confidence": 0.9},
        ],
    )
    db = _db()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result)

    got_row, pieces = await ps._load_stored_plan(db, 7)
    assert got_row is row
    assert [(p.start_page, p.end_page) for p in pieces] == [(1, 2), (3, 5)]

    # corrupt plan (coverage gap) → ignored, not fatal
    row.proposal = [{"start_page": 1, "end_page": 1}]
    got_row, pieces = await ps._load_stored_plan(db, 7)
    assert got_row is row and pieces is None

    # missing page_count → cannot validate → ignored
    row.page_count = 0
    _, pieces = await ps._load_stored_plan(db, 7)
    assert pieces is None


# ---------------------------------------------------------------------------
# persisted per-part resolutions (resume never re-renders resolved parts)
# ---------------------------------------------------------------------------

async def test_execute_split_uses_recorded_resolutions_on_resume(monkeypatch):
    """A part resolved in run 1 (recorded document_id on the plan row) is
    NEVER re-rendered on resume — pdfium bytes are not run-deterministic, so
    re-rendering a DUPLICATE-covered part would mint a near-duplicate child."""
    db = _db()
    parent = _parent()
    plan_row = SimpleNamespace(
        proposal=[
            {"start_page": 1, "end_page": 2, "document_id": 55},  # resolved run 1
            {"start_page": 3, "end_page": 5},
        ]
    )
    resolved_doc = SimpleNamespace(id=55)
    new_child = SimpleNamespace(id=102, split_from_document_id=None)

    async def fake_get(model, pk):
        return {55: resolved_doc, 102: new_child}.get(pk)

    db.get = AsyncMock(side_effect=fake_get)
    calls = _wire_split(
        monkeypatch,
        ingest_results=[IngestResult(IngestStatus.INGESTED, document_id=102)],
    )

    out = await execute_split(
        db, parent, [_piece(1, 2), _piece(3, 5)], plan_row=plan_row
    )

    assert out == [55, 102]
    assert len(calls["ingest"]) == 1  # only the unresolved part
    assert [
        (p.start_page, p.end_page) for c in calls["split_calls"] for p in c
    ] == [(3, 5)]
    # the newly resolved part was recorded too
    assert plan_row.proposal[1]["document_id"] == 102
