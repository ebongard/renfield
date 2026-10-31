"""Blank pages fold into the document before them (``pdf_split_blank_fold_enabled``).

Measured 2026-10-31: 24-33 % of scanned pages are blank duplex back sides. The
boundary model kept proposing them as one-page documents of their own at low
confidence -- 8 of 30 batches sat in ``split_review`` because of it (xidra 668:
segments 2-2, 10-10, 12-12, 16-16 at 0.50, all blank), and with high confidence
a blank page was filed as a document (household 509 -> child 511).

Pure logic plus one real render: page signals are constructed directly, the
boundary model is a fake client, and the blank detector runs on a PDF written
by PIL. No LLM, no DB.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.pdf_split_detector as det
from services.pdf_split_detector import (
    VERDICT_MULTI,
    VERDICT_SINGLE,
    PageSignal,
    SplitPiece,
    classify_slow_lane,
    detect_boundaries,
)
from utils.config import settings

pytestmark = [pytest.mark.unit]


@pytest.fixture
def fold_on(monkeypatch):
    monkeypatch.setattr(settings, "pdf_split_blank_fold_enabled", True)


@pytest.fixture
def fold_off(monkeypatch):
    monkeypatch.setattr(settings, "pdf_split_blank_fold_enabled", False)


def _text(page: int) -> PageSignal:
    return PageSignal(page=page, text=f"Rechnung Nr. {page} vom 01.02.2026", quality_ok=True)


def _blank(page: int) -> PageSignal:
    return PageSignal(page=page, text=det._PLACEHOLDER_BLANK, quality_ok=True, blank=True)


def _payload(*ranges):
    """ranges: (start, end, confidence)."""
    return {
        "documents": [
            {
                "start_page": s,
                "end_page": e,
                "title": f"Doc {s}-{e}",
                "doc_type": "letter",
                "confidence": c,
            }
            for s, e, c in ranges
        ]
    }


def _client(payload):
    client = AsyncMock()
    client.chat = AsyncMock(
        return_value=SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))
    )
    return client


def _ranges(verdict):
    return [(p.start_page, p.end_page) for p in verdict.pieces]


@pytest.mark.asyncio
class TestDetectBoundariesFold:
    async def test_blank_back_side_mid_document_joins_the_preceding_document(self, fold_on):
        # Letter on 1-2, the blank back of page 2 on 3, invoice on 4-5. The
        # model makes the blank page a document of its own at 0.50.
        signals = [_text(1), _text(2), _blank(3), _text(4), _text(5)]
        client = _client(_payload((1, 2, 0.95), (3, 3, 0.5), (4, 5, 0.92)))

        verdict = await detect_boundaries(signals, llm_client=client)

        assert verdict.kind == VERDICT_MULTI
        assert _ranges(verdict) == [(1, 3), (4, 5)]
        assert [p.title for p in verdict.pieces] == ["Doc 1-2", "Doc 4-5"]

    async def test_blank_page_heading_a_piece_moves_to_the_preceding_one(self, fold_on):
        # The model starts the invoice on its blank page: a blank page cannot
        # start a document.
        signals = [_text(1), _blank(2), _text(3), _text(4)]
        client = _client(_payload((1, 1, 0.9), (2, 4, 0.9)))

        verdict = await detect_boundaries(signals, llm_client=client)

        assert _ranges(verdict) == [(1, 2), (3, 4)]

    async def test_alternating_blank_pages(self, fold_on):
        # Single-sided stack fed duplex: every even page is a blank back.
        signals = [_text(p) if p % 2 else _blank(p) for p in range(1, 9)]
        client = _client(
            _payload(*[(p, p, 0.9 if p % 2 else 0.5) for p in range(1, 9)])
        )

        verdict = await detect_boundaries(signals, llm_client=client)

        assert _ranges(verdict) == [(1, 2), (3, 4), (5, 6), (7, 8)]
        assert verdict.min_confidence == pytest.approx(0.9)

    async def test_blank_first_page_attaches_to_the_following_document(self, fold_on):
        signals = [_blank(1), _text(2), _text(3), _text(4)]
        client = _client(_payload((1, 1, 0.5), (2, 3, 0.9), (4, 4, 0.9)))

        verdict = await detect_boundaries(signals, llm_client=client)

        assert _ranges(verdict) == [(1, 3), (4, 4)]
        assert verdict.pieces[0].title == "Doc 2-3"

    async def test_all_blank_batch_is_one_document_without_an_llm_call(self, fold_on):
        client = _client(_payload((1, 1, 0.5), (2, 2, 0.5)))

        verdict = await detect_boundaries([_blank(p) for p in range(1, 5)], llm_client=client)

        assert verdict.kind == VERDICT_SINGLE
        client.chat.assert_not_called()

    async def test_a_nearly_blank_signature_page_is_not_folded(self, fold_on):
        # A page carrying only a signature line is NOT blank (the detector saw
        # ink), so it stays a document of its own if the model says so.
        signature = PageSignal(page=2, text="Unterschrift", quality_ok=False)
        signals = [_text(1), signature, _text(3)]
        client = _client(_payload((1, 1, 0.9), (2, 2, 0.9), (3, 3, 0.9)))

        verdict = await detect_boundaries(signals, llm_client=client)

        assert _ranges(verdict) == [(1, 1), (2, 2), (3, 3)]

    async def test_a_blank_only_piece_no_longer_holds_the_batch_under_the_threshold(
        self, fold_on
    ):
        """The xidra-668 shape: real documents well above the threshold, the
        blank pieces at 0.50 -- before the fold, min_confidence was 0.50 and
        the batch waited for review."""
        signals = [_text(1), _blank(2), _text(3), _text(4), _blank(5), _text(6)]
        client = _client(
            _payload((1, 1, 0.93), (2, 2, 0.5), (3, 4, 0.88), (5, 5, 0.5), (6, 6, 0.97))
        )

        verdict = await detect_boundaries(signals, llm_client=client)

        assert _ranges(verdict) == [(1, 2), (3, 5), (6, 6)]
        # Neither lowered nor raised: each real piece keeps its own value.
        assert [p.confidence for p in verdict.pieces] == [0.93, 0.88, 0.97]
        assert verdict.min_confidence >= settings.pdf_split_auto_threshold

    async def test_the_fold_never_raises_a_real_piece_over_the_threshold(self, fold_on):
        signals = [_text(1), _blank(2), _text(3)]
        client = _client(_payload((1, 1, 0.6), (2, 2, 0.5), (3, 3, 0.95)))

        verdict = await detect_boundaries(signals, llm_client=client)

        assert _ranges(verdict) == [(1, 2), (3, 3)]
        assert verdict.min_confidence == pytest.approx(0.6)
        assert verdict.min_confidence < settings.pdf_split_auto_threshold

    async def test_folding_down_to_one_piece_is_a_single_verdict(self, fold_on):
        signals = [_text(1), _text(2), _blank(3)]
        client = _client(_payload((1, 2, 0.9), (3, 3, 0.5)))

        verdict = await detect_boundaries(signals, llm_client=client)

        assert verdict.kind == VERDICT_SINGLE

    async def test_flag_off_keeps_the_blank_piece(self, fold_off):
        # Byte-identical pre-fold behaviour: the model's pieces stand.
        signals = [_text(1), _text(2), _blank(3), _text(4), _text(5)]
        client = _client(_payload((1, 2, 0.95), (3, 3, 0.5), (4, 5, 0.92)))

        verdict = await detect_boundaries(signals, llm_client=client)

        assert _ranges(verdict) == [(1, 2), (3, 3), (4, 5)]
        assert verdict.min_confidence == pytest.approx(0.5)


class TestFoldBlankPages:
    @staticmethod
    def _p(s, e, c=0.9):
        return SplitPiece(start_page=s, end_page=e, title=f"{s}", doc_type="", confidence=c)

    def test_every_page_is_kept_and_coverage_stays_contiguous(self):
        pieces = [self._p(1, 1), self._p(2, 3), self._p(4, 4), self._p(5, 7)]
        out = det.fold_blank_pages(pieces, {2, 4, 5})
        # 2 heads piece 2-3 -> to piece 1; 4 is blank-only -> to 3; 5 heads
        # piece 5-7 -> to 3-4.
        assert [(p.start_page, p.end_page) for p in out] == [(1, 2), (3, 5), (6, 7)]

    def test_no_blank_pages_is_a_no_op(self):
        pieces = [self._p(1, 2), self._p(3, 4)]
        assert det.fold_blank_pages(pieces, set()) == pieces


class TestBlankSignalsFromTheRenderedPage:
    """The detector itself: near-empty text layer AND no ink on the render."""

    @staticmethod
    def _pdf(tmp_path, *draws):
        from PIL import Image, ImageDraw

        pages = []
        for draw in draws:
            im = Image.new("RGB", (850, 1100), "white")
            if draw:
                draw(ImageDraw.Draw(im))
            pages.append(im)
        path = tmp_path / "stack.pdf"
        pages[0].save(path, "PDF", save_all=True, append_images=pages[1:])
        return str(path)

    def test_white_page_is_blank_signature_page_is_not(self, tmp_path, fold_on):
        def signature(d):
            d.line([(450, 900), (780, 900)], fill="black", width=2)
            d.line([(470, 880), (520, 850), (560, 895), (610, 860), (660, 890)],
                   fill="black", width=3)

        path = self._pdf(tmp_path, None, signature)

        signals = det._signals_from_page_texts(["", "Unterschrift"], path)

        assert [s.blank for s in signals] == [True, False]
        assert signals[0].text == det._PLACEHOLDER_BLANK
        assert signals[0].quality_ok is True
        assert signals[0].to_dict()["blank"] is True
        assert "blank" not in signals[1].to_dict()

    def test_more_than_the_char_floor_is_never_blank(self, tmp_path, fold_on):
        # A white render with a real text layer is not blank: text wins.
        path = self._pdf(tmp_path, None)
        long_text = "Sehr geehrte Damen und Herren, anbei"  # > 30 visible chars

        signals = det._signals_from_page_texts([long_text], path)

        assert signals[0].blank is False

    def test_unrenderable_page_is_never_blank(self, tmp_path, fold_on):
        signals = det._signals_from_page_texts([""], str(tmp_path / "missing.pdf"))
        assert signals[0].blank is False
        assert signals[0].quality_ok is False

    def test_flag_off_renders_nothing(self, tmp_path, fold_off, monkeypatch):
        render = MagicMock(return_value=set())
        monkeypatch.setattr(det, "_rendered_blank_pages", render)

        signals = det._signals_from_page_texts([""], str(tmp_path / "x.pdf"))

        render.assert_not_called()
        assert signals[0].blank is False
        assert "blank" not in signals[0].to_dict()

    def test_a_blank_first_page_no_longer_forces_the_vlm_lane(self, fold_on):
        signals = [_blank(1), _text(2), _text(3), _text(4)]
        assert classify_slow_lane(signals) is None


@pytest.mark.asyncio
async def test_vlm_confirmed_blank_page_is_marked_blank(monkeypatch, fold_on):
    monkeypatch.setattr(settings, "ollama_vision_model", "qwen-vl")
    monkeypatch.setattr(det, "_render_page_b64", MagicMock(return_value="b64"))
    monkeypatch.setattr(det, "_page_is_blank", MagicMock(return_value=True))
    svc = MagicMock()
    svc.extract_text_from_image = AsyncMock(return_value="")

    result = await det.vlm_fill_signals(
        "/x.pdf", [PageSignal(page=1, text="?", quality_ok=False)], ollama_service=svc
    )

    assert result.signals[0].blank is True
