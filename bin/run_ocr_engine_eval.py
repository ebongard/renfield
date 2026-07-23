#!/usr/bin/env python3
"""OCR-engine benchmark harness — TODOS.md "OCR engine evaluation / swap".

Answers the question the TODO poses: the KB ingest OCR path is already
docling-based (``services/document_processor.py``: docling ``EasyOcrOptions`` for
force-full-page OCR + a poppler ``pdftotext -layout`` text-layer path, with
``ocr_engine`` ∈ ``docling`` / ``docling_full_page_ocr`` / ``poppler_text_layer``).
Is **Tesseract** (or, someday, a cloud engine) better THAN docling-EasyOcr on the
corpus of documents the pipeline already flagged as low-quality?

It benchmarks a fixed set of OCR engines over the OCR-quality-flagged / chunkless
document population and reports, per engine, how much low-quality-gate-passing
text each yields relative to the current default (``docling``).

Corpus selection (``--limit`` / ``--doc-id`` / ``--all-flagged``) reuses the exact
signals the product already uses:
  * the Paperless-audit low-quality tab
    (``ha_glue/services/paperless_audit_service.py``): ``status='failed' AND
    error_message LIKE 'ocr_quality%'`` OR the latest ``document_processing_history``
    row dropped >= 30% of its chunks at the quality gate, AND
  * the chunkless population ``internal.ingest_status`` /
    ``internal.reindex_documents`` operate on (``status='completed'`` with 0 chunk
    rows).

Engines (offline-first):
  * ``docling``               — current default (docling standard: embedded text +
                                OCR for bitmap regions). THE BASELINE.
  * ``docling_full_page_ocr`` — docling EasyOcr ``force_full_page_ocr=True``.
  * ``poppler_text_layer``    — ``pdftotext -layout`` text layer → simple chunks.
  * ``docling_tesseract``     — docling ``TesseractCliOcrOptions`` /
                                ``TesseractOcrOptions``, the main alternative under test.
  * ``cloud_ocr_stub``        — gated behind ``--cloud``; a NO-OP stub with a TODO.
                                Never calls any cloud API (offline-first).

Every engine's chunks go through the SAME production chunker
(``DocumentProcessor._create_chunks``) and the SAME quality gate
(``utils.content_quality.is_low_quality_text``) as a live ingest, so the counts
are comparable and the "usable text" number means the same thing it does in prod.

METRIC — RELATIVE, NOT GROUND-TRUTH. No human transcripts exist for these docs,
so there is no character-error-rate. The comparable signal is: which engine puts
the MOST low-quality-gate-passing text into the corpus (``usable_chars`` = total
characters across chunks that clear ``is_low_quality_text``) at the LOWEST
drop-ratio. A doc is "improved" vs the baseline when a candidate yields materially
more usable text, "regressed" when it yields materially less. This measures
"more gate-passing text", NOT "more CORRECT text" — an engine that confidently
emits fluent-but-wrong OCR would score well here. Treat the verdict as a
screening signal that says "worth a human spot-check", never as a swap decision
on its own. See docs/design/ocr-engine-eval.md.

Runs on the .159 build box against the prod DB + uploads PVC (backend tests do
not run in CI — see memory/reference_test_runner_159.md). Heavy deps (docling,
easyocr, tesseract, the DB engine) are imported lazily so ``--self-test`` and
``--help`` work on any machine.

Usage (inside the backend image / on .159, PYTHONPATH=src/backend):
    python bin/run_ocr_engine_eval.py --limit 20
    python bin/run_ocr_engine_eval.py --doc-id 1234 --doc-id 1235
    python bin/run_ocr_engine_eval.py --all-flagged --json /tmp/ocr-eval.json
    python bin/run_ocr_engine_eval.py --limit 20 --cloud   # includes the no-op stub
    python bin/run_ocr_engine_eval.py --self-test          # pure-logic, no IO/deps
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("run_ocr_engine_eval")

# Engines benchmarked by default. Order = display order; docling is the baseline.
BASELINE_ENGINE = "docling"
DEFAULT_ENGINES = [
    "docling",
    "docling_full_page_ocr",
    "poppler_text_layer",
    "docling_tesseract",
]
CLOUD_ENGINE = "cloud_ocr_stub"

# Relative "materially different" band for the per-doc improved/unchanged/regressed
# verdict on usable_chars (5%). Below this, two engines are called equal — noise
# in the OCR/chunker is comfortably under 5% on a fixed document.
_IMPROVE_EPS = 0.05


def _add_backend_to_path() -> None:
    """Put ``src/backend`` on sys.path (mirrors bin/backfill_*.py)."""
    backend = Path(__file__).resolve().parent.parent / "src" / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))


# --------------------------------------------------------------------------
# Per-(doc, engine) result + pure aggregation (self-test exercises this half)
# --------------------------------------------------------------------------

@dataclass
class EngineResult:
    engine: str
    ok: bool = False              # engine ran and produced a result
    available: bool = True        # engine's dependency is installed
    note: str = ""                # skip / error / unavailable reason
    chars_extracted: int = 0      # length of the full extracted text
    chunks_kept: int = 0          # chunks that PASSED the quality gate
    chunks_dropped: int = 0       # chunks the quality gate dropped
    usable_chars: int = 0         # total chars across gate-passing chunks
    wall_s: float = 0.0           # convert + chunk wall-time

    @property
    def total_chunks(self) -> int:
        return self.chunks_kept + self.chunks_dropped

    @property
    def drop_ratio(self) -> float:
        return (self.chunks_dropped / self.total_chunks) if self.total_chunks else 0.0


@dataclass
class DocResult:
    doc_id: int
    filename: str
    file_type: str
    engines: dict[str, EngineResult] = field(default_factory=dict)


def _classify(base_usable: int, cand_usable: int) -> str:
    """Per-doc verdict of a candidate vs the baseline on usable (gate-passing)
    chars: 'improved' / 'regressed' / 'unchanged'. Both-zero is 'unchanged'
    (neither extracted anything — a candidate can't be credited for that)."""
    if base_usable == 0 and cand_usable == 0:
        return "unchanged"
    if cand_usable > base_usable * (1 + _IMPROVE_EPS):
        return "improved"
    if cand_usable < base_usable * (1 - _IMPROVE_EPS):
        return "regressed"
    return "unchanged"


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def aggregate(doc_results: list[DocResult], engines: list[str]) -> dict:
    """Per-engine rollup + improved/unchanged/regressed vs the baseline.

    Pure over ``DocResult`` objects so it is unit-testable without docling/DB.
    """
    summary: dict[str, dict] = {}
    for engine in engines:
        runs = [d.engines[engine] for d in doc_results
                if engine in d.engines and d.engines[engine].ok]
        unavailable = any(
            engine in d.engines and not d.engines[engine].available for d in doc_results
        )
        improved = unchanged = regressed = comparable = 0
        if engine != BASELINE_ENGINE:
            for d in doc_results:
                base = d.engines.get(BASELINE_ENGINE)
                cand = d.engines.get(engine)
                if not (base and base.ok and cand and cand.ok):
                    continue
                comparable += 1
                verdict = _classify(base.usable_chars, cand.usable_chars)
                improved += verdict == "improved"
                unchanged += verdict == "unchanged"
                regressed += verdict == "regressed"
        summary[engine] = {
            "docs_run": len(runs),
            "docs_total": len(doc_results),
            "unavailable": unavailable,
            "mean_drop_ratio": _mean([r.drop_ratio for r in runs]),
            "mean_wall_s": _mean([r.wall_s for r in runs]),
            "mean_usable_chars": _mean([float(r.usable_chars) for r in runs]),
            "vs_baseline": {
                "comparable": comparable,
                "improved": improved,
                "unchanged": unchanged,
                "regressed": regressed,
            },
        }
    return summary


# --------------------------------------------------------------------------
# Corpus selection (reuses the audit + kb-maintenance signals)
# --------------------------------------------------------------------------

async def _select_corpus(doc_ids: list[int] | None, all_flagged: bool,
                         limit: int) -> list[tuple[int, str, str, str]]:
    """Return ``[(doc_id, file_path, filename, file_type), ...]`` for the corpus.

    ``--doc-id`` takes any document verbatim (so an operator can benchmark a
    specific problem doc). Otherwise the corpus is the union of the two
    low-quality signals + the chunkless population, newest first, capped by
    ``limit`` unless ``--all-flagged``.
    """
    from sqlalchemy import Float, and_, cast, func, or_, select  # noqa: PLC0415

    from models.database import Document, DocumentChunk  # noqa: PLC0415
    from services.database import AsyncSessionLocal  # noqa: PLC0415

    # Reuse the audit constant + latest-history subquery so the two can't drift.
    from ha_glue.services.paperless_audit_service import (  # noqa: PLC0415
        _LOW_QUALITY_DROP_RATIO,
        PaperlessAuditService,
    )
    from models.database import DocumentProcessingHistory as _DPH  # noqa: PLC0415

    async with AsyncSessionLocal() as db:
        if doc_ids:
            rows = (
                await db.execute(
                    select(Document.id, Document.file_path, Document.filename,
                           Document.file_type)
                    .where(Document.id.in_(doc_ids))
                    .order_by(Document.id)
                )
            ).all()
            return [(r[0], r[1], r[2], r[3] or "") for r in rows]

        # Signal A: failed on OCR-quality grounds.
        signal_failed = and_(
            Document.status == "failed",
            Document.error_message.like("ocr_quality%"),
        )
        # Signal B: latest history dropped >= 30% of its chunks (audit's rule).
        latest = PaperlessAuditService._latest_history_subquery()
        produced = func.coalesce(_DPH.chunks_produced, 0)
        dropped = func.coalesce(_DPH.chunks_dropped_low_quality, 0)
        denom = func.nullif(produced + dropped, 0)
        signal_drop = (cast(dropped, Float) / denom) >= _LOW_QUALITY_DROP_RATIO
        # Signal C: completed but 0 chunk rows (the chunkless population).
        chunk_sub = (
            select(DocumentChunk.document_id).group_by(DocumentChunk.document_id).subquery()
        )
        signal_chunkless = and_(
            Document.status == "completed",
            chunk_sub.c.document_id.is_(None),
        )

        stmt = (
            select(Document.id, Document.file_path, Document.filename, Document.file_type)
            .select_from(Document)
            .outerjoin(latest, latest.c.document_id == Document.id)
            .outerjoin(_DPH, _DPH.id == latest.c.history_id)
            .outerjoin(chunk_sub, chunk_sub.c.document_id == Document.id)
            .where(or_(signal_failed, signal_drop, signal_chunkless))
            .order_by(Document.id.desc())
        )
        if not all_flagged:
            stmt = stmt.limit(limit)
        rows = (await db.execute(stmt)).all()
        return [(r[0], r[1], r[2], r[3] or "") for r in rows]


# --------------------------------------------------------------------------
# Engine runners — all reuse DocumentProcessor internals READ-ONLY. The
# production process_document() path is untouched.
# --------------------------------------------------------------------------

def _build_tesseract_converter(settings):
    """Build a docling converter using Tesseract OCR, or return (None, reason).

    Prefers ``TesseractCliOcrOptions`` (shells out to the ``tesseract`` binary —
    the most portable). Falls back to ``TesseractOcrOptions`` (the tesserocr
    Python binding). Returns ``(converter, "")`` on success or ``(None, reason)``
    if docling lacks the option class or the tesseract binary is absent — the
    caller then marks the engine unavailable and skips it (never crashes).

    NB: Tesseract language codes are ISO 639-2 (``deu``/``eng``), unlike EasyOcr's
    ``de``/``en``. The ``deu``/``eng`` traineddata must be installed on the host.
    """
    try:
        from docling.datamodel.base_models import InputFormat  # noqa: PLC0415
        from docling.datamodel.pipeline_options import PdfPipelineOptions  # noqa: PLC0415
        from docling.document_converter import (  # noqa: PLC0415
            DocumentConverter,
            PdfFormatOption,
        )
    except ImportError as e:
        return None, f"docling not importable: {e}"

    tess_options = None
    # CLI variant first — needs the `tesseract` binary on PATH.
    if shutil.which("tesseract") is not None:
        try:
            from docling.datamodel.pipeline_options import (  # noqa: PLC0415
                TesseractCliOcrOptions,
            )
            tess_options = TesseractCliOcrOptions(
                lang=["deu", "eng"], force_full_page_ocr=True
            )
        except Exception:  # noqa: BLE001 - fall through to the tesserocr binding
            tess_options = None
    if tess_options is None:
        # tesserocr python binding variant (no CLI needed, but needs the C lib).
        try:
            from docling.datamodel.pipeline_options import TesseractOcrOptions  # noqa: PLC0415
            tess_options = TesseractOcrOptions(
                lang=["deu", "eng"], force_full_page_ocr=True
            )
        except ImportError as e:
            return None, f"tesseract not available (no CLI on PATH, no tesserocr): {e}"

    try:
        pipeline = PdfPipelineOptions()
        pipeline.ocr_options = tess_options
        pipeline.images_scale = settings.rag_ocr_images_scale
        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline)}
        )
        return converter, ""
    except Exception as e:  # noqa: BLE001 - any docling init failure => unavailable
        return None, f"tesseract converter build failed: {e}"


def _result_from_doc(engine: str, dp, doc, chars_extracted: int, t0: float) -> EngineResult:
    """Chunk a converted docling document with the PRODUCTION chunker + gate."""
    chunk_result = dp._create_chunks(doc)
    kept = chunk_result["chunks"]
    dropped = int(chunk_result.get("dropped_low_quality", 0))
    return EngineResult(
        engine=engine,
        ok=True,
        chars_extracted=chars_extracted,
        chunks_kept=len(kept),
        chunks_dropped=dropped,
        usable_chars=sum(len(c.get("text", "")) for c in kept),
        wall_s=round(time.monotonic() - t0, 2),
    )


def _run_engine(engine: str, dp, file_path: str, tess_converter,
                is_low_quality_text) -> EngineResult:
    """Run one engine on one file. Never raises — errors become an ok=False row."""
    t0 = time.monotonic()
    try:
        if engine == "docling":
            res = dp._convert_document(file_path)
            if res is None:
                return EngineResult(engine, note="conversion returned None")
            doc = res.document
            text = doc.export_to_text() if hasattr(doc, "export_to_text") else ""
            return _result_from_doc(engine, dp, doc, len(text or ""), t0)

        if engine == "docling_full_page_ocr":
            res = dp._convert_document_ocr(file_path)
            if res is None:
                return EngineResult(engine, note="OCR conversion returned None")
            doc = res.document
            text = doc.export_to_text() if hasattr(doc, "export_to_text") else ""
            return _result_from_doc(engine, dp, doc, len(text or ""), t0)

        if engine == "poppler_text_layer":
            text = dp.extract_text_layer(file_path)
            if not text:
                return EngineResult(
                    engine, ok=True, note="no usable text layer (non-PDF / no binary / empty)"
                )
            raw = dp._simple_chunk(text)
            kept = [c for c in raw if not is_low_quality_text(c.get("text", ""))]
            return EngineResult(
                engine=engine,
                ok=True,
                chars_extracted=len(text),
                chunks_kept=len(kept),
                chunks_dropped=len(raw) - len(kept),
                usable_chars=sum(len(c.get("text", "")) for c in kept),
                wall_s=round(time.monotonic() - t0, 2),
            )

        if engine == "docling_tesseract":
            if tess_converter is None:
                return EngineResult(engine, available=False, note="tesseract unavailable")
            res = tess_converter.convert(file_path)
            doc = res.document
            text = doc.export_to_text() if hasattr(doc, "export_to_text") else ""
            return _result_from_doc(engine, dp, doc, len(text or ""), t0)

        if engine == CLOUD_ENGINE:
            # TODO(ocr-cloud): wire a cloud OCR engine here IFF the offline-first
            # constraint is relaxed (see docs/design/ocr-engine-eval.md). Must be
            # opt-in, must not exfiltrate document bytes off-box by default, and
            # must be a deliberate, documented deployment decision. This stub
            # NEVER calls any external API.
            return EngineResult(engine, available=False, note="cloud stub — no-op (offline-first)")

        return EngineResult(engine, note=f"unknown engine {engine!r}")
    except Exception as e:  # noqa: BLE001 - one engine choking must not abort the run
        logger.warning("engine %s failed on %s: %s", engine, Path(file_path).name, e)
        return EngineResult(engine, note=f"error: {e}")


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def _print_per_doc(doc_results: list[DocResult], engines: list[str]) -> None:
    print(f"\n{'=' * 96}\nPER-DOCUMENT (usable = gate-passing chars; drop = chunks dropped by quality gate)\n{'=' * 96}")
    for d in doc_results:
        print(f"\ndoc {d.doc_id}  [{d.file_type or '?'}]  {d.filename}")
        print(f"  {'engine':24} {'usable':>9} {'chars':>9} {'kept':>5} {'drop':>5} "
              f"{'drop%':>6} {'wall_s':>7}  note")
        for engine in engines:
            r = d.engines.get(engine)
            if r is None:
                continue
            if not r.ok:
                print(f"  {engine:24} {'—':>9} {'—':>9} {'—':>5} {'—':>5} "
                      f"{'—':>6} {'—':>7}  {r.note}")
                continue
            print(f"  {engine:24} {r.usable_chars:>9} {r.chars_extracted:>9} "
                  f"{r.chunks_kept:>5} {r.chunks_dropped:>5} {r.drop_ratio:>6.0%} "
                  f"{r.wall_s:>7.2f}  {r.note}")


def _print_summary(summary: dict, engines: list[str]) -> None:
    print(f"\n{'=' * 96}\nAGGREGATE (per engine; improved/unchanged/regressed vs "
          f"'{BASELINE_ENGINE}' by usable chars)\n{'=' * 96}")
    print(f"{'engine':24} {'run':>4} {'mean_usable':>12} {'mean_drop':>10} "
          f"{'mean_wall':>10}  {'impr/unch/regr (of comparable)':>32}")
    print("-" * 96)
    for engine in engines:
        s = summary[engine]
        vb = s["vs_baseline"]
        vs = (f"{vb['improved']}/{vb['unchanged']}/{vb['regressed']} "
              f"(n={vb['comparable']})") if engine != BASELINE_ENGINE else "— baseline —"
        unavailable = "  [UNAVAILABLE on some/all docs]" if s["unavailable"] else ""
        print(f"{engine:24} {s['docs_run']:>4} "
              f"{str(s['mean_usable_chars']):>12} {str(s['mean_drop_ratio']):>10} "
              f"{str(s['mean_wall_s']):>10}  {vs:>32}{unavailable}")
    print("-" * 96)
    print("Reminder: this is a RELATIVE, ground-truth-free screen (most gate-passing "
          "text wins).\nIt does NOT measure OCR CORRECTNESS — confirm any candidate "
          "with a human spot-check\nbefore swapping. Decision rule: see docs/design/ocr-engine-eval.md.")


# --------------------------------------------------------------------------
# self-test — pure aggregation logic, no docling / DB / tesseract
# --------------------------------------------------------------------------

def _self_test() -> int:
    def er(engine, usable, kept, dropped, wall):
        return EngineResult(engine=engine, ok=True, usable_chars=usable,
                            chunks_kept=kept, chunks_dropped=dropped, wall_s=wall)

    docs = [
        DocResult(1, "a.pdf", "pdf", engines={
            "docling": er("docling", 100, 5, 5, 1.0),            # drop 50%
            "docling_tesseract": er("docling_tesseract", 200, 10, 0, 2.0),  # improved
        }),
        DocResult(2, "b.pdf", "pdf", engines={
            "docling": er("docling", 100, 10, 0, 1.0),
            "docling_tesseract": er("docling_tesseract", 100, 10, 0, 2.0),  # unchanged (<5%)
        }),
        DocResult(3, "c.pdf", "pdf", engines={
            "docling": er("docling", 100, 10, 0, 1.0),
            "docling_tesseract": EngineResult("docling_tesseract", available=False,
                                              note="tesseract unavailable"),  # skipped
        }),
    ]
    summary = aggregate(docs, ["docling", "docling_tesseract"])
    tess = summary["docling_tesseract"]
    assert tess["vs_baseline"] == {"comparable": 2, "improved": 1, "unchanged": 1,
                                   "regressed": 0}, tess["vs_baseline"]
    assert tess["docs_run"] == 2, tess["docs_run"]
    assert tess["unavailable"] is True
    assert summary["docling"]["mean_drop_ratio"] == round((0.5 + 0.0 + 0.0) / 3, 2)
    assert _classify(100, 106) == "improved"
    assert _classify(100, 103) == "unchanged"
    assert _classify(100, 90) == "regressed"
    assert _classify(0, 0) == "unchanged"
    # drop_ratio guard: no chunks => 0.0, not a ZeroDivisionError
    assert EngineResult("x", ok=True).drop_ratio == 0.0
    print("self-test OK")
    return 0


# --------------------------------------------------------------------------

def _run(args: argparse.Namespace) -> int:
    import asyncio  # noqa: PLC0415

    _add_backend_to_path()
    from utils.config import settings  # noqa: PLC0415
    from utils.content_quality import is_low_quality_text  # noqa: PLC0415
    from services.document_processor import DocumentProcessor  # noqa: PLC0415

    corpus = asyncio.run(
        _select_corpus(args.doc_id or None, args.all_flagged, args.limit)
    )
    if not corpus:
        logger.warning("empty corpus — no flagged/chunkless documents matched")
        return 1
    logger.info("corpus: %d document(s)", len(corpus))

    engines = list(DEFAULT_ENGINES)
    if args.cloud:
        engines.append(CLOUD_ENGINE)

    # One DocumentProcessor, initialized once (loads docling + EasyOcr models).
    dp = DocumentProcessor()
    dp._ensure_initialized()
    tess_converter, tess_reason = _build_tesseract_converter(settings)
    if tess_converter is None:
        logger.warning("docling_tesseract unavailable — will be skipped: %s", tess_reason)

    doc_results: list[DocResult] = []
    for doc_id, file_path, filename, file_type in corpus:
        if not file_path or not Path(file_path).exists():
            logger.warning("doc %s: file missing on disk (%s) — skipped", doc_id, file_path)
            continue
        dr = DocResult(doc_id=doc_id, filename=filename, file_type=file_type)
        for engine in engines:
            logger.info("doc %s | %s", doc_id, engine)
            dr.engines[engine] = _run_engine(
                engine, dp, file_path, tess_converter, is_low_quality_text
            )
        doc_results.append(dr)

    if not doc_results:
        logger.warning("no documents had readable bytes on disk — nothing benchmarked")
        return 1

    summary = aggregate(doc_results, engines)
    _print_per_doc(doc_results, engines)
    _print_summary(summary, engines)

    if args.json:
        payload = {
            "corpus_size": len(doc_results),
            "engines": engines,
            "tesseract_available": tess_converter is not None,
            "tesseract_reason": tess_reason,
            "documents": [
                {"doc_id": d.doc_id, "filename": d.filename, "file_type": d.file_type,
                 "engines": {e: asdict(r) for e, r in d.engines.items()}}
                for d in doc_results
            ],
            "summary": summary,
        }
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        logger.info("wrote %s", args.json)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--limit", type=int, default=20,
                   help="max flagged/chunkless docs to benchmark (default 20; "
                        "ignored with --all-flagged / --doc-id)")
    p.add_argument("--doc-id", type=int, action="append",
                   help="benchmark a specific document id (repeatable; overrides the "
                        "flagged-corpus selection)")
    p.add_argument("--all-flagged", action="store_true",
                   help="benchmark the ENTIRE flagged/chunkless corpus (no limit)")
    p.add_argument("--cloud", action="store_true",
                   help="include the cloud OCR engine — a NO-OP stub today "
                        "(offline-first; never calls an external API)")
    p.add_argument("--json", help="write full per-doc + summary results to this path")
    p.add_argument("--self-test", action="store_true",
                   help="run the pure aggregation self-test (no DB / docling / tesseract)")
    args = p.parse_args()

    if args.self_test:
        return _self_test()
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
