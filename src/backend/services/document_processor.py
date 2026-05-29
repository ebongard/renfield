"""
Document Processor Service using IBM Docling

Handles document parsing, chunking, and metadata extraction for RAG.
Supports: PDF, DOCX, PPTX, XLSX, HTML, MD, TXT, and images.
"""
import asyncio
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

            logger.info("Initialisiere Docling DocumentConverter (force_full_page_ocr / EasyOCR)...")
            from docling.datamodel.pipeline_options import EasyOcrOptions
            ocr_pipeline_options = PdfPipelineOptions()
            ocr_pipeline_options.ocr_options = EasyOcrOptions(
                lang=["de", "en"],         # Deutsch + Englisch
                force_full_page_ocr=True,  # OCR auf jeder Seite, embedded Text ignoriert
                bitmap_area_threshold=0.0,
            )
            ocr_pipeline_options.images_scale = 2.0  # Höhere Auflösung für bessere OCR-Qualität
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
        """Erkennt garbled/kaputten embedded Text (Leerzeichen-Verhältnis zu niedrig).

        PDFs mit kaputtem Text-Layer enthalten Wörter ohne Leerzeichen
        (z.B. 'UmschauMarktplatz13Wiesbaden'). Normale Texte haben ~15-25%
        Leerzeichen. Unter dem konfigurierten Schwellwert (Standard: 3%)
        wird ein OCR-Re-Lauf empfohlen.
        """
        if not text or len(text) < 50:
            return False
        space_ratio = text.count(' ') / len(text)
        is_garbled = space_ratio < settings.rag_ocr_space_threshold
        if is_garbled:
            logger.warning(
                f"Garbled embedded text detected (space ratio={space_ratio:.1%} "
                f"< threshold={settings.rag_ocr_space_threshold:.1%}) — "
                "re-running with force_full_page_ocr"
            )
        return is_garbled

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

            # Per-chunk-rate trigger (sibling of doc-level _is_text_garbled):
            # if more than rag_chunk_quality_drop_threshold of the emitted
            # chunks failed the quality gate, the OCR run was probably bad
            # at the chunk level even if it cleared the doc-level
            # space-ratio check. Re-convert with force_full_page_ocr.
            #
            # Already-forced short-circuit: if this call was ALREADY
            # invoked with force_ocr=True (or settings.rag_force_ocr) and
            # the result still trips the per-chunk gate, retrying with
            # the same setting will give the same result — fail fast with
            # a distinctive error_message so the maintenance UI can
            # surface it. Prevents the convergence loop the adversarial
            # review flagged.
            #
            # Note on trigger chaining: when the doc-level _is_text_garbled
            # path above (lines 173-188) already re-converted the doc, the
            # NEW chunks may still trip this per-chunk trigger. That re-
            # converts a second time with the same OCR engine — wasted
            # GPU but bounded by the already-forced short-circuit (the
            # second pass below sets force_ocr semantics implicitly via
            # `_convert_document_ocr`). Worst case: 2 OCR retries on the
            # same doc before failing. Acceptable cost for the recovery
            # invariant.
            total_emitted = len(chunks) + dropped
            drop_rate = (dropped / total_emitted) if total_emitted else 0.0
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
                logger.info(
                    f"Per-chunk quality trigger ({dropped}/{total_emitted} = "
                    f"{drop_rate:.0%} > {settings.rag_chunk_quality_drop_threshold:.0%}) "
                    f"— re-konvertiere {path.name} mit force_full_page_ocr"
                )
                ocr_result = await loop.run_in_executor(
                    None, self._convert_document_ocr, file_path
                )
                if ocr_result is None:
                    # The retry converter itself raised — distinct from
                    # "retry produced still-bad chunks". Surfacing the
                    # difference matters for triage: this branch means
                    # the OCR engine choked on the file (corrupt PDF,
                    # OOM, unsupported variant), not that our quality
                    # heuristic is too strict.
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
            # Run both blocking calls off the event loop (executor), matching how
            # Docling conversion is dispatched above — a 60s pdftotext or a large
            # export must not stall concurrent requests.
            docling_text = ""
            if hasattr(doc, "export_to_text"):
                docling_text = await loop.run_in_executor(None, doc.export_to_text)
            field_text = docling_text
            text_layer = await loop.run_in_executor(None, self.extract_text_layer, file_path)
            if text_layer:
                usable, reason = self.assess_text_layer_quality(
                    text_layer, page_count=metadata.get("page_count") or 1
                )
                if usable:
                    field_text = f"{docling_text}\n\n===TEXT-LAYER (raw)===\n{text_layer}"
                else:
                    logger.info(f"Text-layer union skipped for {path.name}: {reason}")

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

    async def extract_text_only(self, file_path: str, max_chars: int = 50000) -> str | None:
        """
        Quick text extraction without chunking or embedding.

        For TXT/MD files, reads directly via aiofiles.
        For other formats, uses Docling conversion + export_to_text().

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
            result = await loop.run_in_executor(None, self._convert_document, file_path)

            if result is None:
                return None

            doc = result.document
            if hasattr(doc, 'export_to_text'):
                text = doc.export_to_text()
                return text[:max_chars] if text else None

            return None

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
