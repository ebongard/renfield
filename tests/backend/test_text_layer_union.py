"""Tests for the text-layer UNION ingest path (T-A0-2).

Covers the field-extraction text-layer quality gate and the raw extractor's guard
paths. The end-to-end recovery (Docling/OCR ∪ text layer on a hybrid PDF) is
validated against the real golden-set doc-44 locally (private data, gitignored);
here we test the deterministic, dependency-free logic.
"""
import pytest

from services.document_processor import DocumentProcessor

pytestmark = pytest.mark.unit


# --- assess_text_layer_quality: the field_text union gate --------------------

def test_usable_normal_text():
    text = (
        "Sehr geehrte Damen und Herren, anbei finden Sie Ihre Rechnung. "
        "Bitte ueberweisen Sie den Betrag bis zum 14.06.2026 ohne Abzug. "
        "Mit freundlichen Gruessen, Ihre Stadtwerke."
    )
    usable, reason = DocumentProcessor.assess_text_layer_quality(text, page_count=1)
    assert usable is True
    assert reason == "usable"


def test_empty_text_layer_rejected():
    usable, reason = DocumentProcessor.assess_text_layer_quality("", page_count=1)
    assert usable is False
    assert "empty" in reason


def test_sparse_text_layer_rejected_as_scan():
    # < min_chars_per_page (50) ⇒ likely an image scan with no/empty text layer
    usable, reason = DocumentProcessor.assess_text_layer_quality("Rechnung 2026", page_count=1)
    assert usable is False
    assert "sparse" in reason or "scan" in reason


def test_no_space_mojibake_rejected():
    text = "UmschauMarktplatz13WiesbadenHauptstrasse99FrankfurtKundennummer4711" * 3
    usable, reason = DocumentProcessor.assess_text_layer_quality(text, page_count=1)
    assert usable is False
    assert "space" in reason


def test_high_replacement_ratio_rejected():
    # broken encoding: many replacement chars, but normal spacing
    text = ("Das ist ein Text mit kaputter Kodierung " + "�" * 6 + " und mehr Inhalt hier ") * 3
    usable, reason = DocumentProcessor.assess_text_layer_quality(text, page_count=1)
    assert usable is False
    assert "replacement" in reason or "encoding" in reason


def test_garbled_glyphs_low_vowel_ratio_rejected():
    # spaced tokens but no vowels ⇒ glyph-mapping garbage
    text = ("bcdfg hjklm npqrs tzwxv jkrnt mvwxz prstq ldknm bvcxz qrtwn ") * 4
    usable, reason = DocumentProcessor.assess_text_layer_quality(text, page_count=1)
    assert usable is False
    assert "vowel" in reason or "garbled" in reason


def test_multipage_coverage_uses_page_count():
    # Same text spread over many pages drops below the per-page coverage floor
    text = "Kurzer Text auf vielen Seiten verteilt. " * 2  # ~80 chars
    assert DocumentProcessor.assess_text_layer_quality(text, page_count=1)[0] is True
    assert DocumentProcessor.assess_text_layer_quality(text, page_count=10)[0] is False


# --- extract_text_layer: guard paths -----------------------------------------

def test_extract_text_layer_non_pdf_returns_empty():
    assert DocumentProcessor.extract_text_layer("/tmp/whatever.txt") == ""


def test_extract_text_layer_missing_binary_returns_empty(monkeypatch):
    import services.document_processor as dp
    monkeypatch.setattr(dp.shutil, "which", lambda _: None)
    assert DocumentProcessor.extract_text_layer("/tmp/some.pdf") == ""


def test_extract_text_layer_truncates_to_cap(monkeypatch):
    """F3: raw text layer is capped to rag_text_layer_max_chars (OOM guard)."""
    import services.document_processor as dp

    class _Proc:
        returncode = 0
        stdout = ("x" * 5000).encode()

    monkeypatch.setattr(dp.shutil, "which", lambda _: "/usr/bin/pdftotext")
    monkeypatch.setattr(dp.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(dp.settings, "rag_text_layer_max_chars", 100)
    out = DocumentProcessor.extract_text_layer("/tmp/big.pdf")
    assert len(out) == 100
