"""Per-chunk OCR-quality gate at ingestion (v2.10.4).

Covers the chunker-layer filter, the per-chunk-rate re-OCR trigger,
the already-forced short-circuit, and the return-shape change of
``DocumentProcessor._create_chunks``. The ingester-layer filter
(``RAGService._ingest_flat`` / ``_ingest_parent_child``) is tested in
``test_rag_service_quality.py`` — keeping the chunker and ingester
concerns in separate files because they're separate subsystems.

Mocks docling everywhere (the chunker iterator + the converters); we
test our filter and trigger logic, not docling's chunking.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# Same docling-stub dance as the existing test_document_processor.py.
_missing_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
    "docling", "docling.document_converter", "docling.chunking",
    "docling.datamodel", "docling.datamodel.pipeline_options",
    "docling.datamodel.base_models",
]
import importlib as _importlib
for _mod in _missing_stubs:
    if _mod in sys.modules:
        continue
    try:
        _importlib.import_module(_mod)
    except Exception:  # noqa: BLE001
        sys.modules[_mod] = MagicMock()


from services.document_processor import DocumentProcessor


# ---- shared fixtures ------------------------------------------------------

def _mock_chunk(text: str, idx: int = 0):
    """Build a docling-shaped chunk mock with a `.text` attribute."""
    c = MagicMock()
    c.text = text
    c.meta = MagicMock()
    c.meta.headings = []
    return c


@pytest.fixture
def processor():
    p = DocumentProcessor()
    # Skip the lazy-init of docling — we set the chunker by hand.
    p._initialized = True
    p._chunker = MagicMock()
    return p


# Real garbage from the production document_chunks table (v2.10.3 sample).
GARBAGE_CHUNK = "- r . : ■ { - n ; ; : t » - , : :' r ' ● r : ; '\nydl .'-Ti'"
CLEAN_CHUNK = (
    "Jutta mag Maracujas und Ananas. Sie isst diese Fruechte fast jeden "
    "Morgen zum Fruehstueck mit Joghurt."
)


# ============================================================ chunker filter
class TestCreateChunksFiltersGarbage:
    def test_returns_new_shape_with_drop_count(self, processor):
        processor._chunker.chunk.return_value = iter([
            _mock_chunk(CLEAN_CHUNK, 0),
        ])
        result = processor._create_chunks(MagicMock())
        assert isinstance(result, dict)
        assert "chunks" in result
        assert "dropped_low_quality" in result
        assert result["dropped_low_quality"] == 0
        assert len(result["chunks"]) == 1
        assert result["chunks"][0]["text"] == CLEAN_CHUNK

    def test_drops_garbage_chunks(self, processor):
        processor._chunker.chunk.return_value = iter([
            _mock_chunk(GARBAGE_CHUNK, 0),
            _mock_chunk(CLEAN_CHUNK, 1),
            _mock_chunk(GARBAGE_CHUNK, 2),
        ])
        result = processor._create_chunks(MagicMock())
        assert result["dropped_low_quality"] == 2
        assert len(result["chunks"]) == 1
        assert result["chunks"][0]["text"] == CLEAN_CHUNK

    def test_all_garbage_returns_empty_chunks(self, processor):
        processor._chunker.chunk.return_value = iter([
            _mock_chunk(GARBAGE_CHUNK, 0),
            _mock_chunk(GARBAGE_CHUNK, 1),
        ])
        result = processor._create_chunks(MagicMock())
        assert result["chunks"] == []
        assert result["dropped_low_quality"] == 2

    def test_empty_text_passes_through(self, processor):
        """Empty/short text is NOT flagged by is_low_quality_text
        (length floor). Such chunks survive _create_chunks and get
        dropped later by the ingester's blank-text check."""
        processor._chunker.chunk.return_value = iter([
            _mock_chunk("", 0),
            _mock_chunk("hi", 1),
        ])
        result = processor._create_chunks(MagicMock())
        assert result["dropped_low_quality"] == 0
        assert len(result["chunks"]) == 2

    def test_warning_log_on_drop(self, processor, caplog):
        import logging
        caplog.set_level(logging.WARNING)
        processor._chunker.chunk.return_value = iter([
            _mock_chunk(GARBAGE_CHUNK, 0),
        ])
        with patch("services.document_processor.logger") as mock_logger:
            processor._create_chunks(MagicMock())
            assert mock_logger.warning.called
            call_msg = mock_logger.warning.call_args.args[0]
            assert "OCR-quality drop" in call_msg
            assert "chunk_idx=0" in call_msg
            assert "preview=" in call_msg


# ===================================================== process_document path
@pytest.mark.asyncio
class TestProcessDocumentReOCRTrigger:
    """The per-chunk-rate trigger: when too many chunks fail the quality
    gate, re-convert with force_full_page_ocr. Mocks the docling
    converters; only tests our trigger + short-circuit logic."""

    async def _setup_processor(self, processor, *, first_chunks, second_chunks=None):
        """Wire both converters to succeed and have the chunker emit
        ``first_chunks`` on its 1st invocation, ``second_chunks`` on its
        2nd. Whether the code calls `_convert_document` or
        `_convert_document_ocr` first depends on whether force_ocr is
        on — both return a valid result object so the next stage runs."""
        result_doc = MagicMock()
        result_doc.document = MagicMock()
        result_doc.document.export_to_text = MagicMock(return_value="x" * 100)
        result_obj = MagicMock(document=result_doc.document)

        # Chunker side-effect: first call → first_chunks, later → second_chunks.
        chunk_calls = {"n": 0}

        def chunk_side_effect(d):
            chunk_calls["n"] += 1
            if chunk_calls["n"] == 1:
                return iter([_mock_chunk(t, i) for i, t in enumerate(first_chunks)])
            return iter([_mock_chunk(t, i) for i, t in enumerate(second_chunks or [])])

        processor._chunker.chunk.side_effect = chunk_side_effect

        # Both converters return a successful result object; the test
        # asserts on call_count to verify which path was taken.
        processor._convert_document = MagicMock(return_value=result_obj)
        processor._convert_document_ocr = MagicMock(return_value=result_obj)

        # _extract_metadata returns a placeholder.
        processor._extract_metadata = MagicMock(
            return_value={"title": "t", "file_type": "pdf"}
        )

    async def test_trigger_fires_when_drop_rate_high(
        self, processor, tmp_path, monkeypatch
    ):
        """First pass: 3/4 garbage (75%) → trigger fires → re-convert
        returns 4/4 clean → success."""
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4")

        await self._setup_processor(
            processor,
            first_chunks=[GARBAGE_CHUNK, GARBAGE_CHUNK, GARBAGE_CHUNK, CLEAN_CHUNK],
            second_chunks=[CLEAN_CHUNK, CLEAN_CHUNK, CLEAN_CHUNK, CLEAN_CHUNK],
        )
        # Disable the legacy doc-level _is_text_garbled path so we
        # isolate the per-chunk trigger.
        monkeypatch.setattr(
            "services.document_processor.settings.rag_ocr_auto_detect", False
        )

        result = await processor.process_document(str(f), force_ocr=False)

        assert result["status"] == "completed"
        assert len(result["chunks"]) == 4
        processor._convert_document_ocr.assert_called_once()

    async def test_already_forced_short_circuits_to_failed(
        self, processor, tmp_path, monkeypatch
    ):
        """force_ocr=True input + bad result → fail fast with the
        distinct error_message. No second re-conversion."""
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4")

        await self._setup_processor(
            processor,
            first_chunks=[GARBAGE_CHUNK, GARBAGE_CHUNK, GARBAGE_CHUNK, CLEAN_CHUNK],
        )
        monkeypatch.setattr(
            "services.document_processor.settings.rag_ocr_auto_detect", False
        )

        result = await processor.process_document(str(f), force_ocr=True)

        assert result["status"] == "failed"
        assert result["error"] == "ocr_quality_low_after_forced_ocr"
        # _convert_document_ocr was called ONCE (the initial conversion
        # because force_ocr=True picks the OCR path); the retry
        # short-circuit prevents a second call.
        assert processor._convert_document_ocr.call_count == 1
        # And the cheap _convert_document was never called.
        processor._convert_document.assert_not_called()

    async def test_second_pass_still_bad_marks_failed(
        self, processor, tmp_path, monkeypatch
    ):
        """First pass bad, retry with force_full_page_ocr, second pass
        STILL bad → status=failed with error_message='ocr_quality_low'."""
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4")

        await self._setup_processor(
            processor,
            first_chunks=[GARBAGE_CHUNK, GARBAGE_CHUNK, GARBAGE_CHUNK, CLEAN_CHUNK],
            second_chunks=[GARBAGE_CHUNK, GARBAGE_CHUNK, GARBAGE_CHUNK, CLEAN_CHUNK],
        )
        monkeypatch.setattr(
            "services.document_processor.settings.rag_ocr_auto_detect", False
        )

        result = await processor.process_document(str(f), force_ocr=False)

        assert result["status"] == "failed"
        assert result["error"] == "ocr_quality_low"
        assert processor._convert_document_ocr.call_count == 1

    async def test_trigger_does_not_fire_below_threshold(
        self, processor, tmp_path, monkeypatch
    ):
        """1/4 garbage (25%) is below the 30% default → no retry."""
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4")

        await self._setup_processor(
            processor,
            first_chunks=[
                CLEAN_CHUNK, CLEAN_CHUNK, CLEAN_CHUNK, GARBAGE_CHUNK,
            ],
        )
        monkeypatch.setattr(
            "services.document_processor.settings.rag_ocr_auto_detect", False
        )

        result = await processor.process_document(str(f), force_ocr=False)

        assert result["status"] == "completed"
        assert len(result["chunks"]) == 3  # 1 dropped
        processor._convert_document_ocr.assert_not_called()

    async def test_retry_converter_failure_marks_doc_failed(
        self, processor, tmp_path, monkeypatch
    ):
        """When the per-chunk-rate trigger fires and _convert_document_ocr
        returns None (the documented failure return), the function MUST
        return status='failed' with a distinct error_message — NOT fall
        through to the success path with the original bad chunks.

        Distinct from test_second_pass_still_bad_marks_failed: that one
        covers \"retry succeeded but produced still-bad chunks\". This one
        covers \"retry converter itself failed\". Different error_message
        because the operator-side remediation differs (engine swap vs.
        threshold tune)."""
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4")

        # First call returns clean obj for the initial conversion;
        # second call (the retry) returns None to simulate converter
        # failure.
        result_doc = MagicMock()
        result_doc.document = MagicMock()
        result_doc.document.export_to_text = MagicMock(return_value="x" * 100)
        result_obj = MagicMock(document=result_doc.document)

        processor._chunker.chunk.return_value = iter(
            [_mock_chunk(t, i) for i, t in enumerate(
                [GARBAGE_CHUNK, GARBAGE_CHUNK, GARBAGE_CHUNK, CLEAN_CHUNK]
            )]
        )
        processor._convert_document = MagicMock(return_value=result_obj)
        processor._convert_document_ocr = MagicMock(return_value=None)  # the bug surface
        processor._extract_metadata = MagicMock(
            return_value={"title": "t", "file_type": "pdf"}
        )
        monkeypatch.setattr(
            "services.document_processor.settings.rag_ocr_auto_detect", False
        )

        result = await processor.process_document(str(f), force_ocr=False)

        assert result["status"] == "failed"
        assert result["error"] == "ocr_retry_conversion_failed"
        # Chunks should NOT be the original garbage — empty list per
        # the failure shape (we threw them out, not ingested).
        assert result["chunks"] == []
        # Sanity: the converter was called exactly once for retry.
        assert processor._convert_document_ocr.call_count == 1


# ============================== text-layer-chunk path (hybrid-PDF OOM/degrade fix)
@pytest.mark.asyncio
class TestProcessDocumentTextLayerChunkPath:
    """When the per-chunk trigger fires but a poppler text layer is USABLE, the
    Docling garbage is a font-decode failure (subsetted no-ToUnicode font), NOT a
    scan. We chunk from the text layer and SKIP force-OCR — recovering positioned
    tokens into the chunks AND avoiding the OOM-prone double conversion."""

    def _wire(self, processor, *, first_chunks, text_layer, tl_usable, second_chunks=None):
        result_obj = MagicMock()
        result_obj.document = MagicMock()
        result_obj.document.export_to_text = MagicMock(return_value="garbage docling export")
        calls = {"n": 0}

        def chunk_side_effect(d):
            calls["n"] += 1
            picked = first_chunks if calls["n"] == 1 else (second_chunks or [])
            return iter([_mock_chunk(t, i) for i, t in enumerate(picked)])

        processor._chunker.chunk.side_effect = chunk_side_effect
        processor._convert_document = MagicMock(return_value=result_obj)
        processor._convert_document_ocr = MagicMock(return_value=result_obj)
        processor._extract_metadata = MagicMock(
            return_value={"title": "t", "file_type": "pdf", "page_count": 1}
        )
        # Shadow the static/class methods on the instance to isolate the decision.
        processor.extract_text_layer = MagicMock(return_value=text_layer)
        processor.assess_text_layer_quality = MagicMock(
            return_value=(tl_usable, "usable" if tl_usable else "garbled")
        )

    async def test_usable_text_layer_chunks_from_poppler_and_skips_ocr(
        self, processor, tmp_path, monkeypatch
    ):
        """3/4 Docling garbage (trigger fires) + usable poppler text layer →
        chunk from the text layer, ocr_engine='poppler_text_layer', and the
        degrading force-OCR pass is NOT run. Recovered tokens land in the CHUNKS
        (not just field_text)."""
        f = tmp_path / "hybrid.pdf"
        f.write_bytes(b"%PDF-1.4")
        clean = (
            "Finanzverwaltung NRW Muenster. Ihre Steuernummer: 114/5876/5293. "
            "Datum: 17.04.2026. Frist zur Aktivierung: 23.07.2026. " * 4
        )
        self._wire(
            processor,
            first_chunks=[GARBAGE_CHUNK, GARBAGE_CHUNK, GARBAGE_CHUNK, CLEAN_CHUNK],
            text_layer=clean,
            tl_usable=True,
        )
        monkeypatch.setattr(
            "services.document_processor.settings.rag_ocr_auto_detect", False
        )

        result = await processor.process_document(str(f), force_ocr=False)

        assert result["status"] == "completed"
        assert result["ocr_engine"] == "poppler_text_layer"
        # The whole point: NO degrading force-OCR pass.
        processor._convert_document_ocr.assert_not_called()
        joined = " ".join(c["text"] for c in result["chunks"])
        assert "114/5876/5293" in joined          # recovered token now retrievable in CHUNKS
        assert "23.07.2026" in joined             # the dropped deadline date too
        assert "114/5876/5293" in result["field_text"]

    async def test_unusable_text_layer_still_force_ocrs(
        self, processor, tmp_path, monkeypatch
    ):
        """Trigger fires but NO usable text layer (genuine scan) → the existing
        force_full_page_ocr path runs unchanged."""
        f = tmp_path / "scan.pdf"
        f.write_bytes(b"%PDF-1.4")
        self._wire(
            processor,
            first_chunks=[GARBAGE_CHUNK, GARBAGE_CHUNK, GARBAGE_CHUNK, CLEAN_CHUNK],
            second_chunks=[CLEAN_CHUNK, CLEAN_CHUNK, CLEAN_CHUNK, CLEAN_CHUNK],
            text_layer="",          # scan → no text layer
            tl_usable=False,
        )
        monkeypatch.setattr(
            "services.document_processor.settings.rag_ocr_auto_detect", False
        )

        result = await processor.process_document(str(f), force_ocr=False)

        assert result["status"] == "completed"
        assert result["ocr_engine"] == "docling_full_page_ocr"
        processor._convert_document_ocr.assert_called_once()

    async def test_auto_detect_then_trigger_takes_poppler_path(
        self, processor, tmp_path, monkeypatch
    ):
        """Chained path the OOM leak lived on: the doc-level auto-detect re-OCR
        fires FIRST (binding ocr_result), THEN the per-chunk trigger fires on the
        re-OCR'd chunks. With a usable text layer it must still take the poppler
        path — no SECOND force-OCR — and ocr_result must be released before
        gc.collect() (regression guard for the lingering-reference leak)."""
        f = tmp_path / "hybrid.pdf"
        f.write_bytes(b"%PDF-1.4")
        # Word-rich enough to clear the per-chunk quality gate (number-heavy text
        # alone trips the low-wordlike-ratio filter — that's the gate working).
        clean = (
            "Finanzverwaltung NRW Muenster. Ihre Steuernummer lautet "
            "114/5876/5293. Die Frist zur Aktivierung endet am 23.07.2026. " * 4
        )
        # First chunking (of the auto-detect re-OCR'd doc) is 3/4 garbage → trigger.
        self._wire(
            processor,
            first_chunks=[GARBAGE_CHUNK, GARBAGE_CHUNK, GARBAGE_CHUNK, CLEAN_CHUNK],
            text_layer=clean,
            tl_usable=True,
        )
        # Force the doc-level auto-detect branch to fire (binds ocr_result).
        processor._is_text_garbled = MagicMock(return_value=True)
        monkeypatch.setattr(
            "services.document_processor.settings.rag_ocr_auto_detect", True
        )

        result = await processor.process_document(str(f), force_ocr=False)

        assert result["status"] == "completed"
        assert result["ocr_engine"] == "poppler_text_layer"
        # Auto-detect re-OCR'd ONCE; the poppler path must not force-OCR again.
        processor._convert_document_ocr.assert_called_once()
        joined = " ".join(c["text"] for c in result["chunks"])
        assert "114/5876/5293" in joined

    async def test_poppler_path_applies_quality_gate(
        self, processor, tmp_path, monkeypatch
    ):
        """The poppler-chunk path runs the SAME per-chunk quality gate as
        _create_chunks. A text layer the assessor called usable but whose
        _simple_chunk output is low-quality is dropped, and
        chunks_dropped_low_quality reflects it (NOT hardcoded 0) — so this path
        can't ingest spans every other path rejects, and keeps the audit signal."""
        f = tmp_path / "hybrid.pdf"
        f.write_bytes(b"%PDF-1.4")
        self._wire(
            processor,
            first_chunks=[GARBAGE_CHUNK, GARBAGE_CHUNK, GARBAGE_CHUNK, CLEAN_CHUNK],
            text_layer=GARBAGE_CHUNK,   # single low-quality segment
            tl_usable=True,
        )
        monkeypatch.setattr(
            "services.document_processor.settings.rag_ocr_auto_detect", False
        )

        result = await processor.process_document(str(f), force_ocr=False)

        assert result["status"] == "completed"
        assert result["ocr_engine"] == "poppler_text_layer"
        assert result["chunks"] == []                     # garbage span was gated out
        assert result["chunks_dropped_low_quality"] == 1  # honest count, not 0

    async def test_poppler_path_frees_memory_before_chunking(
        self, processor, tmp_path, monkeypatch
    ):
        """OOM regression guard: the trigger path must release the Docling
        conversion via gc.collect() before chunking from the text layer. Patches
        gc.collect and asserts it ran — reverting the free+collect lines (the
        actual memory fix) fails this test instead of silently passing."""
        f = tmp_path / "hybrid.pdf"
        f.write_bytes(b"%PDF-1.4")
        clean = "Steuernummer 114/5876/5293, Frist 23.07.2026. " * 6
        self._wire(
            processor,
            first_chunks=[GARBAGE_CHUNK, GARBAGE_CHUNK, GARBAGE_CHUNK, CLEAN_CHUNK],
            text_layer=clean,
            tl_usable=True,
        )
        collect_spy = MagicMock()
        monkeypatch.setattr("services.document_processor.gc.collect", collect_spy)
        monkeypatch.setattr(
            "services.document_processor.settings.rag_ocr_auto_detect", False
        )

        result = await processor.process_document(str(f), force_ocr=False)

        assert result["ocr_engine"] == "poppler_text_layer"
        collect_spy.assert_called()   # conversion freed before chunking from text


def test_ocr_converter_uses_configured_images_scale(monkeypatch):
    """Wiring guard: the force-OCR converter must apply
    settings.rag_ocr_images_scale, not a hardcoded constant. Captures the
    PdfPipelineOptions the OCR converter is built from and asserts images_scale
    came from settings — reverting line 66 to a literal fails this."""
    import types

    import services.document_processor as dp

    captured = types.SimpleNamespace(opts=None)

    class _Opts:
        def __init__(self):
            self.ocr_options = None
            self.images_scale = None
            captured.opts = self

    # docling may be REAL in the test container, so stub the validating/heavy
    # classes (_ensure_initialized imports them locally) and keep only the
    # line-66 wiring under test: PdfPipelineOptions().images_scale = settings.…
    pipeline_mod = sys.modules["docling.datamodel.pipeline_options"]
    conv_mod = sys.modules["docling.document_converter"]
    chunk_mod = sys.modules["docling.chunking"]
    monkeypatch.setattr(pipeline_mod, "PdfPipelineOptions", _Opts, raising=False)
    monkeypatch.setattr(
        pipeline_mod, "EasyOcrOptions", lambda **k: MagicMock(), raising=False
    )
    monkeypatch.setattr(conv_mod, "PdfFormatOption", MagicMock, raising=False)
    monkeypatch.setattr(conv_mod, "DocumentConverter", MagicMock, raising=False)
    monkeypatch.setattr(chunk_mod, "HybridChunker", MagicMock, raising=False)
    monkeypatch.setattr(dp.settings, "rag_ocr_images_scale", 1.25)

    p = dp.DocumentProcessor()
    p._ensure_initialized()

    assert captured.opts is not None
    assert captured.opts.images_scale == 1.25


def test_images_scale_config_default_within_bounds():
    """OOM knob: the force-OCR raster scale is configurable and defaults to a
    value that keeps the re-conversion within the 6Gi limit."""
    from utils.config import settings

    assert 0.5 <= settings.rag_ocr_images_scale <= 4.0
    assert settings.rag_ocr_images_scale == 1.5
