"""Unit tests for multi-document PDF boundary detection.

Pure logic — no pdfium, no LLM, no DB: page signals are constructed directly
and the boundary model is a fake client. Covers the hard requirement that page
count is NEVER a gate/cap/signal, the validation matrix, slow-lane routing,
and window batching with the open-trailing-piece carry.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

import services.pdf_split_detector as det
from services.pdf_split_detector import (
    SLOW_REASON_VLM,
    SLOW_REASON_WINDOWS,
    VERDICT_MULTI,
    VERDICT_SINGLE,
    PageSignal,
    classify_slow_lane,
    detect_boundaries,
    validate_boundaries,
)

pytestmark = [pytest.mark.unit]


def _sig(page: int, text: str = "Rechnung Nr. 42 vom 01.02.2026", ok: bool = True):
    return PageSignal(page=page, text=text, quality_ok=ok)


def _payload(*ranges, conf=0.9):
    return {
        "documents": [
            {
                "start_page": s,
                "end_page": e,
                "title": f"Doc {s}-{e}",
                "doc_type": "letter",
                "confidence": conf,
            }
            for s, e in ranges
        ]
    }


# ---------------------------------------------------------------------------
# validate_boundaries
# ---------------------------------------------------------------------------

class TestValidateBoundaries:
    def test_valid_multi(self):
        pieces = validate_boundaries(_payload((1, 2), (3, 3), (4, 9)), 1, 9)
        assert [(p.start_page, p.end_page) for p in pieces] == [(1, 2), (3, 3), (4, 9)]

    def test_gap_is_rejected(self):
        assert validate_boundaries(_payload((1, 2), (4, 5)), 1, 5) is None

    def test_overlap_is_rejected(self):
        assert validate_boundaries(_payload((1, 3), (3, 5)), 1, 5) is None

    def test_out_of_range_end_is_rejected(self):
        assert validate_boundaries(_payload((1, 2), (3, 7)), 1, 5) is None

    def test_tail_not_covered_is_rejected(self):
        assert validate_boundaries(_payload((1, 2), (3, 4)), 1, 5) is None

    def test_wrong_start_is_rejected(self):
        assert validate_boundaries(_payload((2, 5)), 1, 5) is None

    def test_reversed_range_is_rejected(self):
        assert validate_boundaries(_payload((1, 3), (5, 4)), 1, 5) is None
        # even a single reversed piece
        assert validate_boundaries({"documents": [{"start_page": 3, "end_page": 1}]}, 3, 3) is None

    def test_single_document_is_valid(self):
        pieces = validate_boundaries(_payload((1, 12)), 1, 12)
        assert len(pieces) == 1

    def test_garbage_shapes(self):
        assert validate_boundaries(None, 1, 3) is None
        assert validate_boundaries({}, 1, 3) is None
        assert validate_boundaries({"documents": []}, 1, 3) is None
        assert validate_boundaries({"documents": "nope"}, 1, 3) is None
        assert validate_boundaries({"documents": [1, 2]}, 1, 3) is None
        assert (
            validate_boundaries({"documents": [{"start_page": "x", "end_page": 2}]}, 1, 2)
            is None
        )

    def test_confidence_clamped_and_defaulted(self):
        payload = {
            "documents": [
                {"start_page": 1, "end_page": 1, "confidence": 7},
                {"start_page": 2, "end_page": 2, "confidence": -1},
                {"start_page": 3, "end_page": 3},
            ]
        }
        pieces = validate_boundaries(payload, 1, 3)
        assert [p.confidence for p in pieces] == [1.0, 0.0, 0.0]

    def test_title_and_type_cleaned(self):
        payload = {
            "documents": [
                {"start_page": 1, "end_page": 1, "title": "  X  " * 200, "doc_type": None}
            ]
        }
        pieces = validate_boundaries(payload, 1, 1)
        assert len(pieces[0].title) <= det._MAX_TITLE
        assert pieces[0].doc_type == ""


# ---------------------------------------------------------------------------
# slow-lane classification — fraction-based, NEVER page-count-based
# ---------------------------------------------------------------------------

class TestClassifySlowLane:
    def test_clean_short_file_is_inline(self):
        assert classify_slow_lane([_sig(1), _sig(2)]) is None

    def test_no_min_page_gate_two_pages_participate(self):
        # A 2-page PDF can already be two single-page documents — there must be
        # no minimum-page short-circuit anywhere.
        assert classify_slow_lane([_sig(1), _sig(2)]) is None

    def test_garbage_first_page_routes_to_vlm(self):
        signals = [_sig(1, ok=False)] + [_sig(p) for p in range(2, 12)]
        assert classify_slow_lane(signals) == SLOW_REASON_VLM

    def test_garbage_fraction_routes_to_vlm(self):
        # 4 of 10 garbage (40% > 30%) — fraction, not count.
        signals = [_sig(p, ok=(p > 4)) for p in range(1, 11)]
        signals[0] = _sig(1, ok=True)  # keep page 1 clean; fraction still trips
        signals[1] = _sig(2, ok=False)
        signals[2] = _sig(3, ok=False)
        signals[3] = _sig(4, ok=False)
        signals[4] = _sig(5, ok=False)
        assert classify_slow_lane(signals) == SLOW_REASON_VLM

    def test_small_garbage_fraction_stays_inline(self):
        # 1 of 10 garbage (10%) — inline path handles it via the placeholder.
        signals = [_sig(p, ok=(p != 5)) for p in range(1, 11)]
        assert classify_slow_lane(signals) is None

    def test_oversized_signals_route_to_windows(self, monkeypatch):
        monkeypatch.setattr(det.settings, "pdf_split_window_chars", 200)
        signals = [_sig(p, text="x" * 120) for p in range(1, 5)]
        assert classify_slow_lane(signals) == SLOW_REASON_WINDOWS

    def test_empty_signals_are_inline_noop(self):
        assert classify_slow_lane([]) is None


# ---------------------------------------------------------------------------
# detect_boundaries — fake LLM, incl. window batching + carry
# ---------------------------------------------------------------------------

def _fake_client(payloads: list[dict | str]):
    """A client whose chat() returns each payload in turn (dicts are dumped).
    Response shape mirrors ollama's attribute-access object (see
    utils.llm_client.extract_response_content)."""
    from types import SimpleNamespace

    responses = [
        SimpleNamespace(
            message=SimpleNamespace(
                content=p if isinstance(p, str) else json.dumps(p)
            )
        )
        for p in payloads
    ]
    client = AsyncMock()
    client.chat = AsyncMock(side_effect=responses)
    return client


@pytest.mark.asyncio
class TestDetectBoundaries:
    async def test_empty_signals_single(self):
        verdict = await detect_boundaries([], llm_client=_fake_client([]))
        assert verdict.kind == VERDICT_SINGLE

    async def test_single_page_is_analytic_single_no_llm_call(self):
        # One page cannot contain two documents — the verdict is analytic and
        # the LLM round-trip is skipped (NOT a page-count gate: any file with
        # >= 2 pages always goes through content-based detection).
        client = _fake_client([])
        verdict = await detect_boundaries([_sig(1)], llm_client=client)
        assert verdict.kind == VERDICT_SINGLE
        client.chat.assert_not_called()

    async def test_transient_llm_failure_raises_not_single(self):
        # An infra blip (LLM host down) must NOT silently commit a
        # single-document verdict — that would permanently ingest a
        # multi-document PDF as one combined doc (COMPLETED → re-push dedups).
        import httpx

        from services.pdf_split_errors import SplitTransientError

        client = AsyncMock()
        client.chat = AsyncMock(side_effect=httpx.ConnectError("host down"))
        with pytest.raises(SplitTransientError):
            await detect_boundaries([_sig(1), _sig(2)], llm_client=client)

    async def test_single_doc_verdict(self):
        client = _fake_client([_payload((1, 5))])
        verdict = await detect_boundaries([_sig(p) for p in range(1, 6)], llm_client=client)
        assert verdict.kind == VERDICT_SINGLE

    async def test_multi_single_page_docs_back_to_back(self):
        # N one-page documents — the shape a page-count heuristic would miss.
        client = _fake_client([_payload(*[(p, p) for p in range(1, 6)])])
        verdict = await detect_boundaries([_sig(p) for p in range(1, 6)], llm_client=client)
        assert verdict.kind == VERDICT_MULTI
        assert len(verdict.pieces) == 5

    async def test_mixed_single_and_multipage(self):
        # 1-page letter, 6-page contract, 1-page invoice — interleaved shapes.
        client = _fake_client([_payload((1, 1), (2, 7), (8, 8))])
        verdict = await detect_boundaries([_sig(p) for p in range(1, 9)], llm_client=client)
        assert verdict.kind == VERDICT_MULTI
        assert [(p.start_page, p.end_page) for p in verdict.pieces] == [
            (1, 1), (2, 7), (8, 8),
        ]

    async def test_two_page_pdf_can_split(self):
        client = _fake_client([_payload((1, 1), (2, 2))])
        verdict = await detect_boundaries([_sig(1), _sig(2)], llm_client=client)
        assert verdict.kind == VERDICT_MULTI

    async def test_unparseable_response_collapses_to_single(self):
        client = _fake_client(["not json at all"])
        verdict = await detect_boundaries([_sig(1), _sig(2)], llm_client=client)
        assert verdict.kind == VERDICT_SINGLE

    async def test_invalid_coverage_collapses_to_single(self):
        client = _fake_client([_payload((1, 1))])  # page 2 uncovered
        verdict = await detect_boundaries([_sig(1), _sig(2)], llm_client=client)
        assert verdict.kind == VERDICT_SINGLE

    async def test_llm_exception_collapses_to_single(self):
        client = AsyncMock()
        client.chat = AsyncMock(side_effect=RuntimeError("boom"))
        verdict = await detect_boundaries([_sig(1), _sig(2)], llm_client=client)
        assert verdict.kind == VERDICT_SINGLE

    async def test_min_confidence_property(self):
        client = _fake_client(
            [{"documents": [
                {"start_page": 1, "end_page": 1, "confidence": 0.95},
                {"start_page": 2, "end_page": 2, "confidence": 0.4},
            ]}]
        )
        verdict = await detect_boundaries([_sig(1), _sig(2)], llm_client=client)
        assert verdict.kind == VERDICT_MULTI
        assert verdict.min_confidence == pytest.approx(0.4)

    async def test_windowed_open_piece_is_re_decided_by_next_window(self, monkeypatch):
        # Force two windows over 6 pages. Window 1 (pages 1-3) answers
        # [(1,1), (2,3)]; its trailing piece (2,3) is open and must be
        # re-decided by window 2, which is told the carry start (2) and
        # answers [(2,5), (6,6)]. Final result: (1,1), (2,5), (6,6).
        signals = [_sig(p, text="x" * 60) for p in range(1, 7)]
        line_cost = len(det._signal_line(signals[0])) + 1
        monkeypatch.setattr(det.settings, "pdf_split_window_chars", line_cost * 3)
        client = _fake_client([_payload((1, 1), (2, 3)), _payload((2, 5), (6, 6))])

        verdict = await detect_boundaries(signals, llm_client=client)

        assert verdict.kind == VERDICT_MULTI
        assert [(p.start_page, p.end_page) for p in verdict.pieces] == [
            (1, 1), (2, 5), (6, 6),
        ]
        assert client.chat.await_count == 2
        # The second call's user prompt must carry the open document's start.
        second_user = client.chat.await_args_list[1].kwargs["messages"][1]["content"]
        assert "Seite 2" in second_user

    async def test_boundary_exactly_on_window_edge(self, monkeypatch):
        # Window 1 (pages 1-3) ends exactly where a document ends: it answers
        # [(1,3)] — one open piece spanning the whole window. Window 2 is told
        # carry=1 and closes it at 3, then adds (4,6).
        signals = [_sig(p, text="x" * 60) for p in range(1, 7)]
        line_cost = len(det._signal_line(signals[0])) + 1
        monkeypatch.setattr(det.settings, "pdf_split_window_chars", line_cost * 3)
        client = _fake_client([_payload((1, 3)), _payload((1, 3), (4, 6))])

        verdict = await detect_boundaries(signals, llm_client=client)

        assert verdict.kind == VERDICT_MULTI
        assert [(p.start_page, p.end_page) for p in verdict.pieces] == [(1, 3), (4, 6)]

    async def test_single_window_is_one_llm_call(self):
        client = _fake_client([_payload((1, 2), (3, 4))])
        verdict = await detect_boundaries([_sig(p) for p in range(1, 5)], llm_client=client)
        assert verdict.kind == VERDICT_MULTI
        assert client.chat.await_count == 1


# ---------------------------------------------------------------------------
# window batching mechanics
# ---------------------------------------------------------------------------

class TestBuildWindows:
    def test_single_window_common_case(self):
        signals = [_sig(p) for p in range(1, 10)]
        assert len(det._build_windows(signals)) == 1

    def test_every_page_in_exactly_one_window(self, monkeypatch):
        signals = [_sig(p, text="x" * 50) for p in range(1, 31)]
        line_cost = len(det._signal_line(signals[0])) + 1
        monkeypatch.setattr(det.settings, "pdf_split_window_chars", line_cost * 7)
        windows = det._build_windows(signals)
        assert len(windows) > 1
        flat = [s.page for w in windows for s in w]
        assert flat == list(range(1, 31))

    def test_oversized_single_page_still_gets_a_window(self, monkeypatch):
        monkeypatch.setattr(det.settings, "pdf_split_window_chars", 10)
        windows = det._build_windows([_sig(1, text="x" * 500)])
        assert len(windows) == 1 and len(windows[0]) == 1


# ---------------------------------------------------------------------------
# snippet building
# ---------------------------------------------------------------------------

class TestSnippets:
    def test_short_text_kept_whole(self):
        assert det._snippet("Kurzer Text") == "Kurzer Text"

    def test_long_text_keeps_head_and_tail(self):
        text = "A" * 1000 + " MITTE " + "Z" * 1000
        snip = det._snippet(text)
        assert snip.startswith("A")
        assert snip.endswith("Z")
        assert " … " in snip
        assert len(snip) <= det._SIGNAL_HEAD_CHARS + det._SIGNAL_TAIL_CHARS + 10

    def test_whitespace_squashed(self):
        assert det._snippet("a\t\t b   c") == "a b c"
