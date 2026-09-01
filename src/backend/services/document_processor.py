"""
Document Processor Service using IBM Docling

Handles document parsing, chunking, and metadata extraction for RAG.
Supports: PDF, DOCX, PPTX, XLSX, HTML, MD, TXT, and images.
"""
import asyncio
import gc
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiofiles
from loguru import logger

from utils.config import settings


class DocumentProcessor:
    """
    Prozessiert Dokumente mit IBM Docling für RAG.

    Docling bietet strukturierte Dokumentenextraktion mit:
    - Layout-Erkennung (Tabellen, Formeln, Code-Blöcke)
    - OCR für gescannte Dokumente (inkl. force_full_page_ocr für garbled PDFs)
    - Metadaten-Extraktion

    OCR-Verhalten (konfigurierbar via Settings):
    - Standard: Docling nutzt embedded Text + OCR für Bitmap-Regionen
    - rag_force_ocr=True: Immer force_full_page_ocr (embedded Text ignoriert)
    - rag_ocr_auto_detect=True: Erkennt garbled Text (Leerzeichen-Anteil < Schwellwert)
      und wiederholt die Konvertierung mit force_full_page_ocr
    """

    def __init__(self):
        self._converter = None
        self._ocr_converter = None   # Converter mit force_full_page_ocr=True
        self._chunker = None
        self._initialized = False

    def _build_full_page_ocr_options(self):
        """OCR options for the force_full_page_ocr re-run converter, per settings.

        ``rag_ocr_engine='tesseract'`` (default; the 2026-07 148-doc eval winner over
        EasyOcr — 111/148 improved, 8 regressed, quality-gate drop 0.68→0.30, no speed
        penalty) prefers the ``tesseract`` CLI and falls back to the ``tesserocr``
        binding. If the tesseract **runtime** is not actually usable it **fails safe to
        EasyOcr** so ingest never crashes. ``'easyocr'`` keeps the legacy engine.

        Tesseract lang codes are ISO 639-2 (``deu``/``eng``); EasyOcr uses ``de``/``en``.
        """
        from docling.datamodel.pipeline_options import EasyOcrOptions

        engine = (settings.rag_ocr_engine or "tesseract").strip().lower()
        if engine == "tesseract":
            tess = self._build_tesseract_ocr_options()
            if tess is not None:
                return tess
            logger.warning(
                "rag_ocr_engine='tesseract', aber die Tesseract-Runtime ist nicht nutzbar "
                "(CLI+deu/eng-traineddata oder tesserocr-Binding fehlen) — Fallback auf EasyOcr"
            )

        logger.info("Initialisiere force_full_page_ocr-Converter (Engine: EasyOcr de+en)")
        return EasyOcrOptions(lang=["de", "en"], force_full_page_ocr=True, bitmap_area_threshold=0.0)

    def _build_tesseract_ocr_options(self):
        """Tesseract OCR options, or ``None`` if the runtime can't actually OCR deu+eng.

        Verifies the **runtime**, not just that the docling options class constructs —
        ``TesseractCliOcrOptions``/``TesseractOcrOptions`` are config-only and build even
        when tesseract is absent, which would then crash at *convert* time. So the CLI
        variant requires the ``tesseract`` binary on PATH AND the deu/eng traineddata;
        the binding variant requires the ``tesserocr`` module to be importable. Returns
        ``None`` when neither holds so the caller fails safe to EasyOcr.
        """
        import shutil

        if shutil.which("tesseract") is not None and self._tesseract_cli_has_langs():
            try:
                from docling.datamodel.pipeline_options import TesseractCliOcrOptions
                opts = TesseractCliOcrOptions(lang=["deu", "eng"], force_full_page_ocr=True)
                logger.info("Initialisiere force_full_page_ocr-Converter (Engine: Tesseract-CLI deu+eng)")
                return opts
            except Exception:  # noqa: BLE001 - fall through to the binding
                pass

        import importlib.util
        if importlib.util.find_spec("tesserocr") is not None:
            try:
                from docling.datamodel.pipeline_options import TesseractOcrOptions
                opts = TesseractOcrOptions(lang=["deu", "eng"], force_full_page_ocr=True)
                logger.info("Initialisiere force_full_page_ocr-Converter (Engine: tesserocr deu+eng)")
                return opts
            except Exception:  # noqa: BLE001 - binding present but options unbuildable
                pass

        return None

    @staticmethod
    def _tesseract_cli_has_langs():
        """True iff the ``tesseract`` CLI reports both ``deu`` and ``eng`` traineddata."""
        import subprocess
        try:
            res = subprocess.run(
                ["tesseract", "--list-langs"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:  # noqa: BLE001 - binary missing / unrunnable
            return False
        langs = (res.stdout or "") + (res.stderr or "")
        return "deu" in langs and "eng" in langs

    def _ensure_initialized(self):
        """Lazy initialization von Docling (lädt Modelle beim ersten Aufruf)"""
        if self._initialized:
            return

        try:
            from docling.chunking import HybridChunker
            from docling.datamodel.pipeline_options import OcrAutoOptions, PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.base_models import InputFormat

            logger.info("Initialisiere Docling DocumentConverter (Standard)...")
            self._converter = DocumentConverter()

            ocr_pipeline_options = PdfPipelineOptions()
            ocr_pipeline_options.ocr_options = self._build_full_page_ocr_options()
            ocr_pipeline_options.images_scale = settings.rag_ocr_images_scale  # memory-vs-accuracy; see config
            self._ocr_converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=ocr_pipeline_options)
                }
            )

            logger.info("Initialisiere Docling HybridChunker...")
            self._chunker = HybridChunker(
                tokenizer="sentence-transformers/all-MiniLM-L6-v2",
                max_tokens=settings.rag_chunk_size,
                merge_peers=True,  # Merge adjacent chunks of same type
            )

            self._initialized = True
            logger.info("Docling erfolgreich initialisiert")

        except ImportError as e:
            logger.error(f"Docling nicht installiert: {e}")
            raise RuntimeError(
                "Docling ist nicht installiert. "
                "Bitte 'pip install docling docling-core' ausführen."
            ) from e
        except Exception as e:
            logger.error(f"Fehler beim Initialisieren von Docling: {e}")
            raise

    _VOWELS = set("aeiouäöüAEIOUÄÖÜ")

    @staticmethod
    def extract_text_layer(file_path: str) -> str:
        """Raw PDF text-layer extraction via poppler ``pdftotext -layout``.

        Recovers positioned text-layer tokens (e.g. a right-aligned deadline date
        rendered in a subsetted no-ToUnicode font) that the Docling/OCR stack drops.
        Verified as the ONLY extractor that recovers such tokens (pymupdf and Docling
        both miss the MacRoman/uni=no case — see tasks/T-A0-1-RESULTS). Used to build
        the field-extraction UNION; NOT used for RAG chunking.

        Returns "" for non-PDFs, when ``pdftotext`` is unavailable, or on any error
        (the caller then falls back to Docling/OCR output alone — no hard failure).
        """
        if not str(file_path).lower().endswith(".pdf"):
            return ""
        if shutil.which("pdftotext") is None:
            logger.warning("pdftotext (poppler-utils) not installed — text-layer union skipped")
            return ""
        try:
            proc = subprocess.run(
                ["pdftotext", "-layout", file_path, "-"],
                capture_output=True, timeout=60, check=False,
            )
            if proc.returncode != 0:
                logger.warning(f"pdftotext exit {proc.returncode} for {Path(file_path).name}")
                return ""
            text = proc.stdout.decode("utf-8", errors="replace")
            cap = settings.rag_text_layer_max_chars
            if len(text) > cap:
                logger.warning(f"text layer truncated to {cap} chars for {Path(file_path).name}")
                text = text[:cap]
            return text
        except Exception as e:  # subprocess timeout / decode / OS error
            logger.warning(f"text-layer extraction failed for {Path(file_path).name}: {e}")
            return ""

    @classmethod
    def assess_text_layer_quality(cls, text: str, page_count: int = 1) -> tuple[bool, str]:
        """Decide whether a raw text layer is trustworthy enough to UNION into
        field_text. Multi-signal (calibrated on the Schicht A golden set, T-A0-1):
        coverage, space ratio, replacement/control-char ratio, vowel-token ratio.

        Returns ``(usable, reason)``. ``usable=False`` ⇒ drop the text layer and use
        Docling/OCR output alone (the text layer is empty/scan or garbled).
        Distinct from ``_is_text_garbled`` (which gates the OCR re-conversion); this
        gates the field-extraction union and is intentionally a separate decision.
        """
        n = len(text)
        if n == 0:
            return False, "empty text layer"
        chars_per_page = n / max(1, page_count)
        if chars_per_page < settings.rag_text_layer_min_chars_per_page:
            return False, f"sparse text layer ({chars_per_page:.0f} chars/page) — likely a scan"
        space_ratio = text.count(" ") / n
        if space_ratio < settings.rag_text_layer_min_space_ratio:
            return False, f"low space ratio ({space_ratio:.1%}) — no-space mojibake"
        repl = text.count("�") + sum(1 for c in text if ord(c) < 9 or 13 < ord(c) < 32)
        if repl / n > settings.rag_text_layer_max_replacement_ratio:
            return False, f"high replacement/control ratio ({repl / n:.1%}) — broken encoding"
        toks = re.findall(r"\S+", text)
        alpha = [t for t in toks if sum(c.isalpha() for c in t) >= 3]
        if alpha:
            vowel_ratio = sum(1 for t in alpha if any(c in cls._VOWELS for c in t)) / len(alpha)
            if vowel_ratio < settings.rag_text_layer_min_vowel_ratio:
                return False, f"low vowel-token ratio ({vowel_ratio:.0%}) — garbled glyphs"
        return True, "usable"

    @staticmethod
    def _is_text_garbled(text: str) -> bool:
        """Thin delegate to the shared ``utils.ocr_quality.is_text_garbled``.

        Kept as a method so existing call sites / tests are unchanged; the
        heuristic itself lives in one place now (shared with the Paperless
        audit's quality score so the two can't drift).
        """
        from utils.ocr_quality import is_text_garbled
        return is_text_garbled(text)

    async def process_document(
        self,
        file_path: str,
        force_ocr: bool = False
    ) -> dict[str, Any]:
        """
        Verarbeitet ein Dokument und extrahiert strukturierte Chunks.

        Args:
            file_path: Pfad zur Dokumentdatei
            force_ocr: OCR auf allen Seiten erzwingen (ignoriert embedded Text).
                       Nützlich für PDFs mit kaputtem Text-Layer.
                       Überschreibt rag_force_ocr und rag_ocr_auto_detect.

        Returns:
            {
                "metadata": {...},
                "chunks": [{"text": ..., "metadata": {...}}, ...],
                "status": "completed" | "failed",
                "error": "..." (optional)
            }
        """
        try:
            # Lazy initialization
            self._ensure_initialized()

            path = Path(file_path)
            if not path.exists():
                return {
                    "metadata": {},
                    "chunks": [],
                    "status": "failed",
                    "error": f"Datei nicht gefunden: {file_path}"
                }

            logger.info(f"Verarbeite Dokument: {path.name}")

            # Bestimme ob force_full_page_ocr genutzt werden soll
            use_ocr = force_ocr or settings.rag_force_ocr

            # Track which OCR pathway actually produced the chunks we'll
            # return. Updated whenever we (re-)convert below. Surfaces in
            # the result dict as `ocr_engine`, consumed by RAGService and
            # written to document_processing_history.ocr_engine. Unlocks
            # the deferred OCR-engine benchmark (group history by engine).
            ocr_engine = "docling_full_page_ocr" if use_ocr else "docling"

            # Dokument in Thread-Pool konvertieren (CPU-intensiv)
            loop = asyncio.get_event_loop()

            if use_ocr:
                logger.info(f"OCR erzwungen für: {path.name}")
                result = await loop.run_in_executor(
                    None, self._convert_document_ocr, file_path
                )
            else:
                result = await loop.run_in_executor(
                    None, self._convert_document, file_path
                )

            if result is None:
                return {
                    "metadata": {},
                    "chunks": [],
                    "status": "failed",
                    "error": "Dokumentkonvertierung fehlgeschlagen"
                }

            doc = result.document

            # Auto-Erkennung garbled Text: wenn aktiviert und PDF, prüfe ob OCR nötig
            if (
                not use_ocr
                and settings.rag_ocr_auto_detect
                and path.suffix.lower() == ".pdf"
            ):
                # Schnellcheck: ersten Chunk-Text auf Leerzeichen prüfen
                sample_text = doc.export_to_text() if hasattr(doc, 'export_to_text') else ""
                if self._is_text_garbled(sample_text):
                    logger.info(f"Re-konvertiere mit force_full_page_ocr: {path.name}")
                    ocr_result = await loop.run_in_executor(
                        None, self._convert_document_ocr, file_path
                    )
                    if ocr_result is not None:
                        result = ocr_result
                        doc = result.document
                        ocr_engine = "docling_full_page_ocr"

            # Metadaten extrahieren
            metadata = self._extract_metadata(doc, file_path)
            logger.info(f"Metadaten extrahiert: {metadata.get('title', path.name)}")

            # Chunks erstellen
            chunk_result = await loop.run_in_executor(
                None,
                self._create_chunks,
                doc
            )
            chunks = chunk_result["chunks"]
            dropped = int(chunk_result.get("dropped_low_quality", 0))
            total_emitted = len(chunks) + dropped
            drop_rate = (dropped / total_emitted) if total_emitted else 0.0

            # Schicht A text-layer extraction, computed ONCE here and reused by BOTH
            # the re-OCR decision below and the field_text union at the end (a single
            # poppler pdftotext pass, off the event loop). extract_text_layer returns
            # "" for non-PDF / no text layer / missing binary, so tl_usable stays
            # False and behaviour is unchanged for those inputs.
            text_layer = await loop.run_in_executor(None, self.extract_text_layer, file_path)
            tl_usable, tl_reason = (False, "absent")
            if text_layer:
                tl_usable, tl_reason = self.assess_text_layer_quality(
                    text_layer, page_count=metadata.get("page_count") or 1
                )

            # Per-chunk-rate trigger (sibling of doc-level _is_text_garbled): when the
            # chunker dropped more than rag_chunk_quality_drop_threshold of the emitted
            # chunks, the conversion that produced them was bad. The RESPONSE depends
            # on WHY:
            #   * text layer USABLE → the Docling garbage is a font-decode failure
            #     (subsetted no-ToUnicode font), NOT a scanned page. force_full_page_ocr
            #     would re-rasterize and DROP the positioned text-layer tokens (deadline
            #     dates, Steuernummer) — the exact Schicht A degradation. Chunk from the
            #     text layer instead and SKIP OCR. Also sidesteps the OOM-prone double
            #     conversion (two converters + page rasters > 6Gi). Trade-off: a hybrid
            #     doc that is BOTH font-broken AND has image-only values loses the
            #     image-only OCR recovery in chunks — rare, since a usable text layer
            #     means the doc is digital, not scanned.
            #   * else → genuinely scanned/garbled. Re-convert with force_full_page_ocr,
            #     FREEING the Standard conversion first so peak memory holds one doc +
            #     rasters, not two. If the retry ALSO trips → status='failed'.
            #
            # Already-forced short-circuit: a call ALREADY invoked with force_ocr=True
            # that still trips would loop on retry → fail fast with a distinct
            # error_message so the maintenance UI can tell "tried our best" apart.
            if drop_rate > settings.rag_chunk_quality_drop_threshold:
                if use_ocr:
                    logger.error(
                        f"OCR-quality still bad after forced full-page OCR: "
                        f"{path.name} (drop_rate={drop_rate:.0%}, "
                        f"dropped={dropped}/{total_emitted})"
                    )
                    return {
                        "metadata": metadata,
                        "chunks": chunks,
                        "status": "failed",
                        "error": "ocr_quality_low_after_forced_ocr",
                    }

                # Both remaining branches discard the Standard conversion. Free it —
                # plus any auto-detect re-OCR tree still held by ocr_result (lines
                # ~263-268) — and gc.collect() BEFORE either chunking from the text
                # layer or launching the force-OCR converter, so peak memory never
                # holds two Docling document trees (the hybrid-doc OOM fix). collect()
                # rather than relying on refcount drop because Docling trees are
                # cycle-heavy (parent<->child node refs) and won't free on rebind.
                result = None
                doc = None
                ocr_result = None
                gc.collect()

                if tl_usable:
                    logger.info(
                        f"Per-chunk quality trigger ({dropped}/{total_emitted} = "
                        f"{drop_rate:.0%}) on {path.name}, but the embedded text layer "
                        f"is usable ({tl_reason}) — chunking from the text layer and "
                        f"skipping force-OCR (avoids dropping positioned tokens)"
                    )
                    # Apply the SAME per-chunk quality gate _create_chunks uses, so
                    # this path can't ingest low-quality spans every other path drops,
                    # and chunks_dropped_low_quality stays an honest audit signal.
                    # A doc-level-usable text layer is overwhelmingly clean, so this
                    # rarely drops anything — it's defense-in-depth, not the main act.
                    from utils.content_quality import is_low_quality_text
                    raw = self._simple_chunk(text_layer)
                    chunks = [c for c in raw if not is_low_quality_text(c.get("text", ""))]
                    dropped = len(raw) - len(chunks)
                    ocr_engine = "poppler_text_layer"
                else:
                    logger.info(
                        f"Per-chunk quality trigger ({dropped}/{total_emitted} = "
                        f"{drop_rate:.0%} > {settings.rag_chunk_quality_drop_threshold:.0%}) "
                        f"— re-konvertiere {path.name} mit force_full_page_ocr"
                    )
                    ocr_result = await loop.run_in_executor(
                        None, self._convert_document_ocr, file_path
                    )
                    if ocr_result is None:
                        # The retry converter itself raised — distinct from "retry
                        # produced still-bad chunks". This branch means the OCR engine
                        # choked on the file (corrupt PDF, OOM, unsupported variant),
                        # not that our quality heuristic is too strict.
                        logger.error(
                            f"OCR retry conversion FAILED (None result) for "
                            f"{path.name} — ingesting nothing, marking failed"
                        )
                        return {
                            "metadata": metadata,
                            "chunks": [],
                            "status": "failed",
                            "error": "ocr_retry_conversion_failed",
                        }
                    doc = ocr_result.document
                    ocr_engine = "docling_full_page_ocr"
                    chunk_result = await loop.run_in_executor(
                        None, self._create_chunks, doc
                    )
                    chunks = chunk_result["chunks"]
                    dropped = int(chunk_result.get("dropped_low_quality", 0))
                    total_emitted = len(chunks) + dropped
                    drop_rate = (dropped / total_emitted) if total_emitted else 0.0
                    if drop_rate > settings.rag_chunk_quality_drop_threshold:
                        logger.error(
                            f"OCR-quality still bad after retry: {path.name} "
                            f"(drop_rate={drop_rate:.0%})"
                        )
                        return {
                            "metadata": metadata,
                            "chunks": chunks,
                            "status": "failed",
                            "error": "ocr_quality_low",
                        }

            logger.info(
                f"Dokument verarbeitet: {len(chunks)} Chunks erstellt "
                + (f"({dropped} dropped as low-quality)" if dropped else "")
            )

            # Field-extraction UNION (Schicht A): Docling/OCR output ∪ raw text layer.
            # Docling recovers OCR-only image values; the raw text layer recovers
            # positioned tokens Docling drops (right-aligned dates, no-ToUnicode fonts).
            # Neither alone is complete on hybrid PDFs. RAG chunking is unaffected; this
            # only populates result["field_text"] for downstream field extractors.
            # text_layer + tl_usable were computed once above (reused, not re-run).
            # On the text-layer-chunk path `doc` is None → docling_text is "" and
            # field_text falls back to the (good) text layer alone.
            docling_text = ""
            if doc is not None and hasattr(doc, "export_to_text"):
                docling_text = await loop.run_in_executor(None, doc.export_to_text)

            # Final recovery tier: OCR still low-quality (rotated / poor scan whose
            # character garble force-OCR can't fix) → vision-model re-OCR + re-chunk
            # from that text. Best-effort + gated; only replaces chunks when the VLM
            # result scores strictly better. Benefits ingest AND reindex, so an audit
            # re-OCR improvement actually reaches the KB (not re-garbled by Tesseract).
            _ocr_text = docling_text or "\n".join(c.get("text", "") for c in chunks)
            # Effective drop-rate of the FINAL chunk set (text-layer or force-OCR
            # path). A high value means most content was dropped even though the
            # SURVIVING text scores clean — the letter-spacing "usable text layer"
            # case that skips force-OCR. Pass it so the VLM coverage trigger can
            # fire regardless of the survivor score.
            _final_total = len(chunks) + dropped
            _final_drop_rate = (dropped / _final_total) if _final_total else 0.0
            _vlm_text = await self._vlm_ocr_fallback(
                file_path, _ocr_text, drop_rate=_final_drop_rate
            )
            if _vlm_text:
                from utils.content_quality import is_low_quality_text

                _vlm_chunks = [
                    c for c in self._simple_chunk(_vlm_text)
                    if not is_low_quality_text(c.get("text", ""))
                ]
                if _vlm_chunks:
                    logger.info(
                        f"VLM re-OCR: replaced {len(chunks)} chunk(s) with "
                        f"{len(_vlm_chunks)} for {path.name}"
                    )
                    chunks = _vlm_chunks
                    dropped = 0
                    ocr_engine = "vlm_fallback"
                    docling_text = _vlm_text

            field_text = docling_text
            if text_layer and tl_usable:
                field_text = (
                    f"{docling_text}\n\n===TEXT-LAYER (raw)===\n{text_layer}"
                    if docling_text else text_layer
                )
            elif text_layer and not tl_usable:
                logger.info(f"Text-layer union skipped for {path.name}: {tl_reason}")

            return {
                "metadata": metadata,
                "chunks": chunks,
                "status": "completed",
                "ocr_engine": ocr_engine,
                "chunks_dropped_low_quality": dropped,
                "field_text": field_text,
            }

        except Exception as e:
            logger.error(f"Fehler beim Verarbeiten von {file_path}: {e}")
            return {
                "metadata": {},
                "chunks": [],
                "status": "failed",
                "error": str(e)
            }

    def _render_pages_b64(self, file_path: str, max_pages: int) -> list[str]:
        """Render the first ``max_pages`` pages to base64 PNGs for the VLM fallback.

        PDFs via pypdfium2 (Docling's own renderer — no extra dep); image files via
        PIL directly. Returns ``[]`` on any failure or unsupported type (the caller
        then keeps the OCR text). Runs in a thread — pdfium is blocking."""
        import base64
        import io

        ext = Path(file_path).suffix.lower().lstrip(".")
        out: list[str] = []
        try:
            if ext == "pdf":
                import pypdfium2 as pdfium

                pdf = pdfium.PdfDocument(file_path)
                try:
                    for i in range(min(len(pdf), max_pages)):
                        pil = pdf[i].render(scale=2.0).to_pil()
                        buf = io.BytesIO()
                        pil.save(buf, format="PNG")
                        out.append(base64.b64encode(buf.getvalue()).decode())
                finally:
                    pdf.close()
            elif ext in ("png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"):
                from PIL import Image

                with Image.open(file_path) as im:
                    buf = io.BytesIO()
                    im.convert("RGB").save(buf, format="PNG")
                    out.append(base64.b64encode(buf.getvalue()).decode())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"VLM re-OCR: page render failed for {file_path}: {e}")
            return []
        return out

    @staticmethod
    def _token_overlap(survivor_text: str, candidate_text: str) -> float:
        """Fraction of the survivor's distinct alnum tokens (len≥3, lowercased)
        that also appear in ``candidate_text``. 1.0 when the survivor has no such
        tokens (nothing to verify → don't block). Anti-hallucination signal for
        the VLM coverage-acceptance path."""
        import re

        def toks(s: str) -> set[str]:
            return {t for t in re.findall(r"[A-Za-z0-9]{3,}", (s or "").lower())}

        survivors = toks(survivor_text)
        if not survivors:
            return 1.0
        return len(survivors & toks(candidate_text)) / len(survivors)

    async def _vlm_ocr_fallback(
        self, file_path: str, ocr_text: str, drop_rate: float | None = None
    ) -> str | None:
        """Vision-model re-OCR when the OCR text is bad (rotated / poor scan).

        Both Tesseract and EasyOCR fail on the same bad pixels; a vision model reads
        such pages and is robust to orientation. Returns better VLM text, or None
        (keep OCR). Gated + best-effort.

        Trigger + acceptance are decided by TWO signals, because OCR garble comes in
        two flavours: (1) internal-punctuation garble ('Bez:-ihl') the cheap
        ``score_ocr_quality`` heuristic catches, and (2) pronounceable pseudo-words
        ('ZOGEOLONIGGY') that character statistics CANNOT tell from real words — only
        language understanding can, so an ``is_ocr_gibberish`` LM check
        (``ocr_vlm_gibberish_gate_enabled``) covers that style. The LM gate is also
        the acceptance test: use the VLM text when the OCR was gibberish and the VLM
        output is readable (their coarse 1-5 scores can tie at 5 for style-2)."""
        if not settings.ocr_vlm_fallback_enabled:
            return None
        try:
            from utils.ocr_quality import score_ocr_quality

            svc = getattr(self, "_ollama_service", None)
            if svc is None:
                from services.ollama_service import OllamaService

                svc = OllamaService()
                self._ollama_service = svc

            old_score, _ = score_ocr_quality(ocr_text or "")
            # Trigger: cheap char signal (style-1), then the LM gibberish gate for the
            # pronounceable-garbage style the char signal is blind to (style-2).
            ocr_bad = old_score <= settings.ocr_vlm_fallback_score_threshold
            if not ocr_bad and settings.ocr_vlm_gibberish_gate_enabled:
                ocr_bad = await svc.is_ocr_gibberish(ocr_text) is True
            # Coverage trigger (style-3): the survivors score clean, but most of the
            # document was dropped as low-quality — a "usable-but-garbled" text layer
            # that skipped force-OCR. The survivor score can't see the lost content;
            # the high drop-rate can. Re-OCR from the page image regardless of score.
            coverage_bad = (
                drop_rate is not None
                and settings.ocr_vlm_coverage_drop_threshold > 0
                and drop_rate > settings.ocr_vlm_coverage_drop_threshold
            )
            if coverage_bad and not ocr_bad:
                logger.info(
                    f"VLM re-OCR coverage trigger: drop_rate={drop_rate:.0%} > "
                    f"{settings.ocr_vlm_coverage_drop_threshold:.0%} (survivor score "
                    f"{old_score} was ok) — re-transcribing from the page image"
                )
            if not ocr_bad and not coverage_bad:
                return None

            loop = asyncio.get_event_loop()
            images = await loop.run_in_executor(
                None, self._render_pages_b64, file_path, settings.ocr_vlm_fallback_max_pages
            )
            if not images:
                return None

            parts: list[str] = []
            for img in images:
                t = await svc.extract_text_from_image(img)
                if t:
                    parts.append(t)
            vlm_text = "\n\n".join(parts).strip()
            if not vlm_text:
                return None

            # Accept when the VLM is strictly better by the char score, OR (for the
            # style-2 tie) when the LM gate confirms the VLM text is readable.
            new_score, _ = score_ocr_quality(vlm_text)
            accept = new_score > old_score
            if not accept and settings.ocr_vlm_gibberish_gate_enabled:
                accept = await svc.is_ocr_gibberish(vlm_text) is False
            # Coverage acceptance: on a coverage-triggered doc the survivors are a
            # small CLEAN fragment, so the char score can tie/beat the fuller VLM
            # text — accept when the VLM is itself readable (not garbled) AND
            # recovers materially MORE content than the survivors.
            coverage_accept = False
            if not accept and coverage_bad:
                readable = new_score > settings.ocr_vlm_fallback_score_threshold
                if readable and settings.ocr_vlm_gibberish_gate_enabled:
                    readable = await svc.is_ocr_gibberish(vlm_text) is False
                much_more = len(vlm_text) > 1.5 * max(1, len(ocr_text or ""))
                # Anti-hallucination: a faithful transcription reproduces the
                # known-correct survivor tokens; a fabrication (invented amounts /
                # boilerplate) does not. Reject a longer-but-wrong VLM output.
                overlap = self._token_overlap(ocr_text or "", vlm_text)
                overlap_ok = overlap >= settings.ocr_vlm_coverage_min_overlap
                coverage_accept = readable and much_more and overlap_ok
                accept = coverage_accept
                if not coverage_accept and much_more and readable and not overlap_ok:
                    logger.info(
                        f"VLM re-OCR coverage REJECTED for {Path(file_path).name}: "
                        f"survivor-token overlap {overlap:.0%} < "
                        f"{settings.ocr_vlm_coverage_min_overlap:.0%} — likely "
                        f"hallucination, keeping OCR text"
                    )
            if accept:
                logger.info(
                    f"VLM re-OCR: using vision transcription for "
                    f"{Path(file_path).name} ({len(images)} page(s), "
                    f"score {old_score}→{new_score}"
                    + (f", coverage-recovered {len(ocr_text or '')}→{len(vlm_text)} chars"
                       if coverage_accept else "")
                    + ")"
                )
                return vlm_text
            logger.info(
                f"VLM re-OCR for {Path(file_path).name} not better — keeping OCR text"
            )
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"VLM re-OCR fallback failed for {file_path}: {e}")
            return None

    def _convert_document(self, file_path: str):
        """Synchrone Dokumentkonvertierung (für Thread-Pool)"""
        try:
            return self._converter.convert(file_path)
        except Exception as e:
            logger.error(f"Konvertierungsfehler: {e}")
            return None

    def _convert_document_ocr(self, file_path: str):
        """Synchrone Dokumentkonvertierung mit force_full_page_ocr (für Thread-Pool).

        Ignoriert den embedded Text-Layer und führt vollständiges OCR auf
        jeder Seite durch. Liefert bessere Ergebnisse bei gescannten PDFs
        mit kaputtem Text-Layer (fehlende Leerzeichen etc.).
        """
        try:
            return self._ocr_converter.convert(file_path)
        except Exception as e:
            logger.error(f"OCR-Konvertierungsfehler: {e}")
            return None

    @staticmethod
    def _strip_upload_hash(name: str) -> str:
        """Remove the 32-char hex upload hash prefix added by the upload handler.

        Uploaded files are stored as ``<sha256[:32]>_<original_name>``.
        This strips that prefix so titles and filenames are human-readable.
        """
        import re
        return re.sub(r'^[a-f0-9]{32}_', '', name)

    def _extract_metadata(self, doc, file_path: str) -> dict[str, Any]:
        """Extrahiert Dokument-Metadaten"""
        path = Path(file_path)

        # Basis-Metadaten
        metadata = {
            "filename": path.name,
            "file_type": path.suffix.lower().lstrip('.'),
            "file_size": path.stat().st_size if path.exists() else 0,
            "processed_at": datetime.now(UTC).replace(tzinfo=None).isoformat()
        }

        # Docling-Metadaten
        try:
            if hasattr(doc, 'name') and doc.name:
                metadata["title"] = self._strip_upload_hash(doc.name)
            else:
                metadata["title"] = self._strip_upload_hash(path.stem)

            if hasattr(doc, 'origin') and doc.origin:
                if hasattr(doc.origin, 'author'):
                    metadata["author"] = doc.origin.author

            # Seitenanzahl (nur für seitenbasierte Dokumente)
            if hasattr(doc, 'pages') and doc.pages:
                metadata["page_count"] = len(doc.pages)
            elif hasattr(doc, 'page_count'):
                metadata["page_count"] = doc.page_count

        except Exception as e:
            logger.warning(f"Fehler beim Extrahieren von Metadaten: {e}")

        return metadata

    def _create_chunks(self, doc) -> dict[str, Any]:
        """Build chunks from a docling document and apply the OCR-quality gate.

        Returns ``{"chunks": list[dict], "dropped_low_quality": int}``.
        The drop count is the signal the caller (``process_document``) uses
        to decide whether the doc as a whole tripped the per-chunk-rate
        threshold and warrants a force-full-page-OCR retry.

        Each dropped chunk emits a WARNING log with the per-chunk
        preview so operators can grep ingestion logs to catch heuristic
        false positives — recovery is reindex + revert. Without that
        audit trail a legitimate short code/formula chunk silently
        vanishes from search.
        """
        from utils.content_quality import is_low_quality_text

        chunks: list[dict[str, Any]] = []
        dropped = 0

        try:
            chunk_iter = self._chunker.chunk(doc)

            for idx, chunk in enumerate(chunk_iter):
                text = chunk.text or ""
                if is_low_quality_text(text):
                    dropped += 1
                    preview = text.strip().replace("\n", " ")[:80]
                    logger.warning(
                        f"🗑️ OCR-quality drop: chunk_idx={idx} "
                        f"preview={preview!r}"
                    )
                    continue
                chunks.append({
                    "text": text,
                    "chunk_index": idx,
                    "metadata": {
                        "headings": self._get_headings(chunk),
                        "chunk_type": self._get_chunk_type(chunk),
                        "page_number": self._get_page_number(chunk),
                    }
                })

        except Exception as e:
            logger.error(f"Fehler beim Chunking: {e}")
            # Fallback: Einfaches Text-Splitting (also gated).
            if hasattr(doc, 'export_to_text'):
                text = doc.export_to_text()
                fallback = self._simple_chunk(text)
                kept: list[dict[str, Any]] = []
                for c in fallback:
                    if is_low_quality_text(c.get("text", "")):
                        dropped += 1
                        preview = (c.get("text") or "").strip().replace("\n", " ")[:80]
                        logger.warning(
                            f"🗑️ OCR-quality drop (fallback): "
                            f"chunk_idx={c.get('chunk_index')} preview={preview!r}"
                        )
                        continue
                    kept.append(c)
                chunks = kept

        return {"chunks": chunks, "dropped_low_quality": dropped}

    def _get_headings(self, chunk) -> list[str]:
        """Extrahiert Überschriften aus Chunk-Metadaten"""
        try:
            if hasattr(chunk, 'meta') and hasattr(chunk.meta, 'headings'):
                return list(chunk.meta.headings) if chunk.meta.headings else []
        except Exception:
            pass
        return []

    def _get_chunk_type(self, chunk) -> str:
        """Ermittelt den Chunk-Typ (paragraph, table, code, etc.)"""
        try:
            if hasattr(chunk, 'meta') and hasattr(chunk.meta, 'doc_items'):
                if chunk.meta.doc_items:
                    item = chunk.meta.doc_items[0]
                    if hasattr(item, 'label'):
                        return item.label.lower()
        except Exception:
            pass
        return "paragraph"

    def _get_page_number(self, chunk) -> int | None:
        """Ermittelt die Seitennummer des Chunks"""
        try:
            if hasattr(chunk, 'meta') and hasattr(chunk.meta, 'doc_items'):
                if chunk.meta.doc_items:
                    item = chunk.meta.doc_items[0]
                    if hasattr(item, 'prov') and item.prov:
                        return item.prov[0].page_no
        except Exception:
            pass
        return None

    def _simple_chunk(self, text: str) -> list[dict[str, Any]]:
        """Fallback: Einfaches Text-Splitting nach Zeichen"""
        chunks = []
        chunk_size = settings.rag_chunk_size * 4  # ~4 chars per token
        overlap = settings.rag_chunk_overlap * 4

        start = 0
        idx = 0

        while start < len(text):
            end = start + chunk_size
            chunk_text = text[start:end]

            # Versuche, an Satzende zu brechen
            if end < len(text):
                last_period = chunk_text.rfind('.')
                last_newline = chunk_text.rfind('\n')
                break_point = max(last_period, last_newline)
                if break_point > chunk_size // 2:
                    chunk_text = chunk_text[:break_point + 1]
                    end = start + break_point + 1

            if chunk_text.strip():
                chunks.append({
                    "text": chunk_text.strip(),
                    "chunk_index": idx,
                    "metadata": {
                        "headings": [],
                        "chunk_type": "paragraph",
                        "page_number": None,
                    }
                })
                idx += 1

            start = end - overlap

        return chunks

    async def extract_text_only(
        self, file_path: str, max_chars: int = 50000, force_ocr: bool = False
    ) -> str | None:
        """
        Quick text extraction without chunking or embedding.

        For TXT/MD files, reads directly via aiofiles. For other formats, uses
        Docling conversion + export_to_text().

        OCR quality recovery — same gates as ``process_document``: honors
        ``rag_force_ocr`` / the ``force_ocr`` arg, and (for PDFs, when
        ``rag_ocr_auto_detect`` is on) re-converts with full-page OCR if the
        embedded text layer is garbled. Callers therefore get the same OCR
        quality as a fresh KB ingest — they no longer silently keep a
        mojibake'd text layer.

        Returns None on error.
        """
        try:
            path = Path(file_path)
            if not path.exists():
                logger.warning(f"extract_text_only: Datei nicht gefunden: {file_path}")
                return None

            ext = path.suffix.lower().lstrip('.')

            # Plain text files: read directly
            if ext in ("txt", "md"):
                async with aiofiles.open(file_path, encoding='utf-8', errors='replace') as f:
                    text = await f.read(max_chars)
                return text

            # Other formats: use Docling converter
            self._ensure_initialized()

            loop = asyncio.get_event_loop()
            use_ocr = force_ocr or settings.rag_force_ocr
            if use_ocr:
                result = await loop.run_in_executor(None, self._convert_document_ocr, file_path)
            else:
                result = await loop.run_in_executor(None, self._convert_document, file_path)

            if result is None:
                return None

            doc = result.document
            text = doc.export_to_text() if hasattr(doc, 'export_to_text') else ""

            # Auto-detect garbled embedded text and re-run with full-page OCR
            # (PDF only) — the recovery path process_document uses, previously
            # missing here despite the docstring's claim.
            if (
                not use_ocr
                and settings.rag_ocr_auto_detect
                and ext == "pdf"
                and self._is_text_garbled(text)
            ):
                logger.info(f"extract_text_only: re-konvertiere mit force_full_page_ocr: {path.name}")
                ocr_result = await loop.run_in_executor(None, self._convert_document_ocr, file_path)
                if ocr_result is not None and hasattr(ocr_result.document, 'export_to_text'):
                    text = ocr_result.document.export_to_text() or text

            # VLM re-OCR fallback: OCR still garbled (rotated / poor scan) → let the
            # vision model transcribe the page(s). No-op unless enabled + still low
            # quality; only used when it scores strictly better.
            vlm_text = await self._vlm_ocr_fallback(file_path, text)
            if vlm_text:
                text = vlm_text

            return text[:max_chars] if text else None

        except Exception as e:
            logger.error(f"extract_text_only Fehler für {file_path}: {e}")
            return None

    def get_supported_formats(self) -> list[str]:
        """Gibt unterstützte Dateiformate zurück"""
        return [
            "pdf",      # PDF Dokumente
            "docx",     # Microsoft Word
            "doc",      # Legacy Word
            "pptx",     # PowerPoint
            "xlsx",     # Excel
            "html",     # HTML Seiten
            "md",       # Markdown
            "txt",      # Plain Text
            "png",      # Bilder (OCR)
            "jpg",
            "jpeg",
        ]

    def is_supported(self, filename: str) -> bool:
        """Prüft, ob ein Dateiformat unterstützt wird"""
        ext = Path(filename).suffix.lower().lstrip('.')
        return ext in self.get_supported_formats()
