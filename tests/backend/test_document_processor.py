"""Tests for DocumentProcessor.

Tests document processing, metadata extraction, chunk creation,
format support, and fallback text splitting.
"""
import sys
from unittest.mock import MagicMock

# Pre-mock modules not available in test environment
_missing_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
    "docling", "docling.document_converter", "docling.chunking",
    "docling.datamodel", "docling.datamodel.pipeline_options",
    "docling.datamodel.base_models",
]
for _mod in _missing_stubs:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from unittest.mock import patch

import pytest

from services.document_processor import DocumentProcessor


def _make_mock_settings(rag_chunk_size=512, rag_chunk_overlap=50,
                         rag_force_ocr=False, rag_ocr_auto_detect=True,
                         rag_ocr_space_threshold=0.03):
    s = MagicMock()
    s.rag_chunk_size = rag_chunk_size
    s.rag_chunk_overlap = rag_chunk_overlap
    s.rag_force_ocr = rag_force_ocr
    s.rag_ocr_auto_detect = rag_ocr_auto_detect
    s.rag_ocr_space_threshold = rag_ocr_space_threshold
    return s


@pytest.fixture
def mock_settings():
    return _make_mock_settings()


@pytest.fixture
def processor():
    return DocumentProcessor()


# ============================================================================
# Initialization Tests
# ============================================================================

@pytest.mark.unit
class TestDocumentProcessorInit:

    def test_init_not_initialized(self, processor):
        assert processor._initialized is False
        assert processor._converter is None
        assert processor._chunker is None

    def test_ensure_initialized_sets_flag(self, processor):
        """After initialization, _initialized is True."""
        mock_converter_cls = MagicMock()
        mock_chunker_cls = MagicMock()

        with patch("services.document_processor.settings") as mock_s:
            mock_s.rag_chunk_size = 512
            with patch.dict(sys.modules, {
                "docling.document_converter": MagicMock(DocumentConverter=mock_converter_cls),
                "docling.chunking": MagicMock(HybridChunker=mock_chunker_cls),
            }):
                processor._ensure_initialized()

        assert processor._initialized is True
        assert processor._converter is not None
        assert processor._chunker is not None

    def test_ensure_initialized_only_once(self, processor):
        """Repeated calls do not re-initialize."""
        processor._initialized = True
        processor._converter = MagicMock()
        processor._chunker = MagicMock()

        original_converter = processor._converter
        processor._ensure_initialized()

        assert processor._converter is original_converter


# ============================================================================
# Format Support Tests
# ============================================================================

@pytest.mark.unit
class TestFormatSupport:

    def test_supported_formats_include_pdf(self, processor):
        assert "pdf" in processor.get_supported_formats()

    def test_supported_formats_include_docx(self, processor):
        assert "docx" in processor.get_supported_formats()

    def test_supported_formats_include_txt(self, processor):
        assert "txt" in processor.get_supported_formats()

    def test_supported_formats_include_md(self, processor):
        assert "md" in processor.get_supported_formats()

    def test_supported_formats_include_images(self, processor):
        formats = processor.get_supported_formats()
        assert "png" in formats
        assert "jpg" in formats
        assert "jpeg" in formats

    def test_is_supported_pdf(self, processor):
        assert processor.is_supported("document.pdf") is True

    def test_is_supported_docx(self, processor):
        assert processor.is_supported("report.docx") is True

    def test_is_supported_txt(self, processor):
        assert processor.is_supported("notes.txt") is True

    def test_is_supported_unsupported_format(self, processor):
        assert processor.is_supported("video.mp4") is False

    def test_is_supported_case_insensitive(self, processor):
        assert processor.is_supported("FILE.PDF") is True

    def test_is_supported_no_extension(self, processor):
        assert processor.is_supported("noextension") is False


# ============================================================================
# Metadata Extraction Tests
# ============================================================================

@pytest.mark.unit
class TestMetadataExtraction:

    def test_extract_metadata_basic_fields(self, processor, tmp_path):
        """Basic metadata includes filename, file_type, file_size, processed_at."""
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf content")

        mock_doc = MagicMock()
        mock_doc.name = "Test Document"
        mock_doc.origin = None
        mock_doc.pages = None

        metadata = processor._extract_metadata(mock_doc, str(test_file))

        assert metadata["filename"] == "test.pdf"
        assert metadata["file_type"] == "pdf"
        assert metadata["file_size"] > 0
        assert "processed_at" in metadata
        assert metadata["title"] == "Test Document"

    def test_extract_metadata_uses_stem_when_no_doc_name(self, processor, tmp_path):
        """Falls back to filename stem when doc has no name."""
        test_file = tmp_path / "my_report.docx"
        test_file.write_bytes(b"content")

        mock_doc = MagicMock()
        mock_doc.name = None
        mock_doc.origin = None
        mock_doc.pages = None

        metadata = processor._extract_metadata(mock_doc, str(test_file))

        assert metadata["title"] == "my_report"

    def test_extract_metadata_with_author(self, processor, tmp_path):
        """Author is extracted from doc.origin."""
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"content")

        mock_doc = MagicMock()
        mock_doc.name = "Test"
        mock_doc.origin = MagicMock()
        mock_doc.origin.author = "John Doe"
        mock_doc.pages = None

        metadata = processor._extract_metadata(mock_doc, str(test_file))

        assert metadata["author"] == "John Doe"

    def test_extract_metadata_with_page_count(self, processor, tmp_path):
        """Page count extracted from doc.pages."""
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"content")

        mock_doc = MagicMock()
        mock_doc.name = "Test"
        mock_doc.origin = None
        mock_doc.pages = [MagicMock(), MagicMock(), MagicMock()]

        metadata = processor._extract_metadata(mock_doc, str(test_file))

        assert metadata["page_count"] == 3


# ============================================================================
# Chunk Creation Tests
# ============================================================================

@pytest.mark.unit
class TestChunkCreation:

    def test_create_chunks_returns_list(self, processor):
        """Chunks are returned as a list of dicts."""
        mock_chunk = MagicMock()
        mock_chunk.text = "Sample text content"
        mock_chunk.meta = MagicMock()
        mock_chunk.meta.headings = ["Chapter 1"]
        mock_chunk.meta.doc_items = []

        processor._chunker = MagicMock()
        processor._chunker.chunk.return_value = [mock_chunk]

        chunks = processor._create_chunks(MagicMock())

        assert len(chunks) == 1
        assert chunks[0]["text"] == "Sample text content"
        assert chunks[0]["chunk_index"] == 0
        assert "metadata" in chunks[0]

    def test_create_chunks_extracts_headings(self, processor):
        mock_chunk = MagicMock()
        mock_chunk.text = "Content"
        mock_chunk.meta.headings = ["Introduction", "Background"]
        mock_chunk.meta.doc_items = []

        processor._chunker = MagicMock()
        processor._chunker.chunk.return_value = [mock_chunk]

        chunks = processor._create_chunks(MagicMock())

        assert chunks[0]["metadata"]["headings"] == ["Introduction", "Background"]

    def test_create_chunks_fallback_on_error(self, processor):
        """Falls back to simple_chunk when chunking fails."""
        mock_doc = MagicMock()
        mock_doc.export_to_text.return_value = "Hello world. This is a test."

        processor._chunker = MagicMock()
        processor._chunker.chunk.side_effect = Exception("chunking error")

        with patch("services.document_processor.settings") as mock_s:
            mock_s.rag_chunk_size = 512
            mock_s.rag_chunk_overlap = 50
            chunks = processor._create_chunks(mock_doc)

        assert len(chunks) >= 1
        assert chunks[0]["text"] == "Hello world. This is a test."

    def test_get_headings_no_meta(self, processor):
        mock_chunk = MagicMock(spec=[])  # No attributes
        assert processor._get_headings(mock_chunk) == []

    def test_get_chunk_type_default_paragraph(self, processor):
        mock_chunk = MagicMock(spec=[])
        assert processor._get_chunk_type(mock_chunk) == "paragraph"

    def test_get_page_number_none_when_no_prov(self, processor):
        mock_chunk = MagicMock(spec=[])
        assert processor._get_page_number(mock_chunk) is None


# ============================================================================
# Simple Chunk Fallback Tests
# ============================================================================

@pytest.mark.unit
class TestSimpleChunk:

    def test_simple_chunk_splits_text(self, processor):
        """Simple chunking splits long text into pieces."""
        with patch("services.document_processor.settings") as mock_s:
            mock_s.rag_chunk_size = 10  # ~40 chars per chunk
            mock_s.rag_chunk_overlap = 2  # ~8 chars overlap

            text = "A" * 100  # 100 chars
            chunks = processor._simple_chunk(text)

        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk["text"]
            assert "chunk_index" in chunk
            assert chunk["metadata"]["chunk_type"] == "paragraph"

    def test_simple_chunk_empty_text(self, processor):
        with patch("services.document_processor.settings") as mock_s:
            mock_s.rag_chunk_size = 512
            mock_s.rag_chunk_overlap = 50
            chunks = processor._simple_chunk("")

        assert chunks == []

    def test_simple_chunk_short_text(self, processor):
        """Short text results in a single chunk."""
        with patch("services.document_processor.settings") as mock_s:
            mock_s.rag_chunk_size = 512
            mock_s.rag_chunk_overlap = 50
            chunks = processor._simple_chunk("Short text.")

        assert len(chunks) == 1
        assert chunks[0]["text"] == "Short text."
        assert chunks[0]["chunk_index"] == 0

    def test_simple_chunk_breaks_at_sentence(self, processor):
        """Prefers to break at sentence boundaries."""
        with patch("services.document_processor.settings") as mock_s:
            mock_s.rag_chunk_size = 10  # ~40 chars
            mock_s.rag_chunk_overlap = 2

            text = "First sentence. Second sentence. Third sentence."
            chunks = processor._simple_chunk(text)

        # First chunk should end at a period
        assert chunks[0]["text"].endswith(".")


# ============================================================================
# Process Document Tests
# ============================================================================

@pytest.mark.unit
class TestProcessDocument:

    @pytest.mark.asyncio
    async def test_process_nonexistent_file(self, processor):
        """Non-existent file returns failed status."""
        processor._initialized = True
        processor._converter = MagicMock()
        processor._chunker = MagicMock()

        result = await processor.process_document("/nonexistent/file.pdf")

        assert result["status"] == "failed"
        assert "nicht gefunden" in result["error"]
        assert result["chunks"] == []

    @pytest.mark.asyncio
    async def test_process_document_conversion_failure(self, processor, tmp_path):
        """Failed conversion returns failed status."""
        test_file = tmp_path / "bad.pdf"
        test_file.write_bytes(b"not a real pdf")

        processor._initialized = True
        processor._converter = MagicMock()
        processor._converter.convert.return_value = None
        processor._chunker = MagicMock()

        # Mock _convert_document directly since it's called via executor
        processor._convert_document = MagicMock(return_value=None)

        result = await processor.process_document(str(test_file))

        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_process_document_exception(self, processor, tmp_path):
        """Exception during processing returns failed status."""
        test_file = tmp_path / "error.pdf"
        test_file.write_bytes(b"content")

        processor._ensure_initialized = MagicMock(side_effect=RuntimeError("Docling crash"))

        result = await processor.process_document(str(test_file))

        assert result["status"] == "failed"
        assert "Docling crash" in result["error"]


# ============================================================================
# OCR / Garbled Text Detection Tests
# ============================================================================

@pytest.mark.unit
class TestIsTextGarbled:
    """Tests for _is_text_garbled() static method."""

    def test_normal_text_not_garbled(self):
        """Normal text with ~20% spaces is not garbled."""
        text = "Das ist ein normaler Text mit genug Leerzeichen zwischen den Wörtern."
        assert DocumentProcessor._is_text_garbled(text) is False

    def test_garbled_text_no_spaces(self):
        """Text with almost no spaces (< 3%) is detected as garbled."""
        # Simulates: "Umschau,Marktplatz13,65183Wiesbaden" style
        garbled = "UmschauMarktplatz13WiesbadenKundennummer4020545AnsprechpartnerAngelaBockhop"
        assert DocumentProcessor._is_text_garbled(garbled) is True

    def test_short_text_not_garbled(self):
        """Short text (< 50 chars) is never considered garbled."""
        short = "Nospaces"
        assert DocumentProcessor._is_text_garbled(short) is False

    def test_empty_text_not_garbled(self):
        """Empty string returns False."""
        assert DocumentProcessor._is_text_garbled("") is False

    def test_threshold_boundary(self):
        """Text exactly at the 3% space threshold."""
        with patch("services.document_processor.settings") as mock_settings:
            mock_settings.rag_ocr_space_threshold = 0.03
            # 2 spaces in 100 chars = 2% < 3%: garbled
            text_2pct = "a" * 48 + " " + "b" * 49 + " " + "c" * 1
            # Only 2 spaces in 100 chars → space_ratio = 2% < 3%
            assert DocumentProcessor._is_text_garbled(text_2pct) is True

            # 4 spaces in 100 chars = 4% > 3%: not garbled
            text_4pct = "a" * 47 + " " + "b" * 47 + " " + "c" * 2 + " " + "d" * 1 + " "
            assert DocumentProcessor._is_text_garbled(text_4pct) is False


@pytest.mark.unit
class TestForceOcrPath:
    """Tests for the force_ocr parameter in process_document()."""

    @pytest.mark.asyncio
    async def test_force_ocr_uses_ocr_converter(self, tmp_path):
        """force_ocr=True routes to _convert_document_ocr()."""
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"pdf content")

        processor = DocumentProcessor()
        processor._initialized = True
        processor._ocr_converter = MagicMock()
        processor._converter = MagicMock()
        processor._chunker = MagicMock()

        mock_result = MagicMock()
        mock_result.document = MagicMock()
        mock_result.document.export_to_text.return_value = "Test text content"
        processor._convert_document_ocr = MagicMock(return_value=mock_result)
        processor._convert_document = MagicMock(return_value=mock_result)
        processor._create_chunks = MagicMock(return_value=[])
        processor._extract_metadata = MagicMock(return_value={})

        with patch("services.document_processor.settings") as mock_settings:
            mock_settings.rag_force_ocr = False
            mock_settings.rag_ocr_auto_detect = False
            mock_settings.rag_ocr_space_threshold = 0.03

            await processor.process_document(str(test_file), force_ocr=True)

        processor._convert_document_ocr.assert_called_once_with(str(test_file))
        processor._convert_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_ocr_uses_standard_converter(self, tmp_path):
        """force_ocr=False and rag_force_ocr=False uses standard converter."""
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"pdf content")

        processor = DocumentProcessor()
        processor._initialized = True
        processor._ocr_converter = MagicMock()
        processor._converter = MagicMock()
        processor._chunker = MagicMock()

        mock_result = MagicMock()
        mock_result.document = MagicMock()
        mock_result.document.export_to_text.return_value = "Normal text with spaces here."
        processor._convert_document_ocr = MagicMock(return_value=mock_result)
        processor._convert_document = MagicMock(return_value=mock_result)
        processor._create_chunks = MagicMock(return_value=[])
        processor._extract_metadata = MagicMock(return_value={})

        with patch("services.document_processor.settings") as mock_settings:
            mock_settings.rag_force_ocr = False
            mock_settings.rag_ocr_auto_detect = False
            mock_settings.rag_ocr_space_threshold = 0.03

            await processor.process_document(str(test_file), force_ocr=False)

        processor._convert_document.assert_called_once_with(str(test_file))
        processor._convert_document_ocr.assert_not_called()

    @pytest.mark.asyncio
    async def test_global_rag_force_ocr_setting(self, tmp_path):
        """rag_force_ocr=True in config triggers OCR even without force_ocr param."""
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"pdf content")

        processor = DocumentProcessor()
        processor._initialized = True
        processor._ocr_converter = MagicMock()
        processor._converter = MagicMock()
        processor._chunker = MagicMock()

        mock_result = MagicMock()
        mock_result.document = MagicMock()
        processor._convert_document_ocr = MagicMock(return_value=mock_result)
        processor._convert_document = MagicMock(return_value=mock_result)
        processor._create_chunks = MagicMock(return_value=[])
        processor._extract_metadata = MagicMock(return_value={})

        with patch("services.document_processor.settings") as mock_settings:
            mock_settings.rag_force_ocr = True
            mock_settings.rag_ocr_auto_detect = False
            mock_settings.rag_ocr_space_threshold = 0.03

            await processor.process_document(str(test_file), force_ocr=False)

        # Global config overrides force_ocr=False
        processor._convert_document_ocr.assert_called_once()
        processor._convert_document.assert_not_called()
