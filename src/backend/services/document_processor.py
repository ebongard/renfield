"""
Document Processor Service using IBM Docling

Handles document parsing, chunking, and metadata extraction for RAG.
Supports: PDF, DOCX, PPTX, XLSX, HTML, MD, TXT, and images.
"""
import os
import asyncio
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
from loguru import logger

from utils.config import settings


class DocumentProcessor:
    """
    Prozessiert Dokumente mit IBM Docling für RAG.

    Docling bietet strukturierte Dokumentenextraktion mit:
    - Layout-Erkennung (Tabellen, Formeln, Code-Blöcke)
    - OCR für gescannte Dokumente
    - Metadaten-Extraktion
    """

    def __init__(self):
        self._converter = None
        self._chunker = None
        self._initialized = False

    def _ensure_initialized(self):
        """Lazy initialization von Docling (lädt Modelle beim ersten Aufruf)"""
        if self._initialized:
            return

        try:
            from docling.document_converter import DocumentConverter
            from docling.chunking import HybridChunker

            logger.info("Initialisiere Docling DocumentConverter...")
            self._converter = DocumentConverter()

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

    async def process_document(self, file_path: str) -> Dict[str, Any]:
        """
        Verarbeitet ein Dokument und extrahiert strukturierte Chunks.

        Args:
            file_path: Pfad zur Dokumentdatei

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

            # Dokument in Thread-Pool konvertieren (CPU-intensiv)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                self._convert_document,
                file_path
            )

            if result is None:
                return {
                    "metadata": {},
                    "chunks": [],
                    "status": "failed",
                    "error": "Dokumentkonvertierung fehlgeschlagen"
                }

            doc = result.document

            # Metadaten extrahieren
            metadata = self._extract_metadata(doc, file_path)
            logger.info(f"Metadaten extrahiert: {metadata.get('title', path.name)}")

            # Chunks erstellen
            chunks = await loop.run_in_executor(
                None,
                self._create_chunks,
                doc
            )

            logger.info(f"Dokument verarbeitet: {len(chunks)} Chunks erstellt")

            return {
                "metadata": metadata,
                "chunks": chunks,
                "status": "completed"
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

    def _extract_metadata(self, doc, file_path: str) -> Dict[str, Any]:
        """Extrahiert Dokument-Metadaten"""
        path = Path(file_path)

        # Basis-Metadaten
        metadata = {
            "filename": path.name,
            "file_type": path.suffix.lower().lstrip('.'),
            "file_size": path.stat().st_size if path.exists() else 0,
            "processed_at": datetime.utcnow().isoformat()
        }

        # Docling-Metadaten
        try:
            if hasattr(doc, 'name') and doc.name:
                metadata["title"] = doc.name
            else:
                metadata["title"] = path.stem

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

    def _create_chunks(self, doc) -> List[Dict[str, Any]]:
        """Erstellt Chunks mit Docling HybridChunker"""
        chunks = []

        try:
            chunk_iter = self._chunker.chunk(doc)

            for idx, chunk in enumerate(chunk_iter):
                chunk_data = {
                    "text": chunk.text,
                    "chunk_index": idx,
                    "metadata": {
                        "headings": self._get_headings(chunk),
                        "chunk_type": self._get_chunk_type(chunk),
                        "page_number": self._get_page_number(chunk),
                    }
                }
                chunks.append(chunk_data)

        except Exception as e:
            logger.error(f"Fehler beim Chunking: {e}")
            # Fallback: Einfaches Text-Splitting
            if hasattr(doc, 'export_to_text'):
                text = doc.export_to_text()
                chunks = self._simple_chunk(text)

        return chunks

    def _get_headings(self, chunk) -> List[str]:
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

    def _get_page_number(self, chunk) -> Optional[int]:
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

    def _simple_chunk(self, text: str) -> List[Dict[str, Any]]:
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

    def get_supported_formats(self) -> List[str]:
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
