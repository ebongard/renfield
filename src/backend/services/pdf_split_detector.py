"""Multi-document PDF boundary detection (docs/design/pdf-split.md).

Decides whether one ingested PDF actually contains several independent
documents (batch scans: invoices, letters, contracts stapled into one file)
and where the boundaries lie. Runs as a document-worker pre-stage on EVERY
PDF when ``pdf_split_enabled`` — page count is deliberately NEVER a gate,
cap, or signal: the input is an arbitrary mix of single-page documents and
multi-page contracts, so boundaries come from content evidence only.

Staged pipeline:

1. Per-page text signals via the pypdfium2 text layer; per-page quality via
   the calibrated ``DocumentProcessor.assess_text_layer_quality``.
2. Files that need slow work — a garbled/absent text layer needing per-page
   VLM transcription, or signals that exceed one LLM context window — are
   classified for the dedicated split lane (``classify_slow_lane``).
3. Strict-JSON boundary call(s) on the TEXT model (never JSON from the VLM —
   the qwen3-vl think-buffer trap, see paperless_metadata_extractor). Long
   inputs are processed in overlapping context-budget windows with an
   open-trailing-piece carry — a batching mechanism, not a size assumption.
4. Pure-Python validation: contiguous, non-overlapping, exhaustive coverage.
   Anything invalid collapses to a single-document verdict (status quo).

Best-effort by design: every public entry point degrades to "single document"
rather than raising — detection must never break ingest.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from services.prompt_manager import prompt_manager
from utils.config import settings
from utils.llm_client import (
    extract_response_content,
    get_classification_chat_kwargs,
    get_default_client,
)

# Snippet shape per page: the header region (letterhead, sender, date, subject)
# and the footer region ("Seite 1 von 3", signatures) carry the boundary
# evidence. Caps bound the prompt, they never gate which pages participate.
_SIGNAL_HEAD_CHARS = 600
_SIGNAL_TAIL_CHARS = 200
# Fraction of garbage pages above which the text layer alone cannot support a
# boundary decision → slow lane for VLM fill-in. Fraction-based on purpose
# (never an absolute page count).
_GARBAGE_FRACTION_SLOW = 0.30
# Overlap is implicit via the open-piece carry; windows advance page-contiguous.

VERDICT_SINGLE = "single"
VERDICT_MULTI = "multi"
VERDICT_NEEDS_SLOW = "needs_slow"

SLOW_REASON_VLM = "vlm"
SLOW_REASON_WINDOWS = "windows"

_PLACEHOLDER_UNREADABLE = "[unlesbare Seite / Scan ohne Textebene]"


@dataclass(frozen=True)
class PageSignal:
    """Boundary evidence for one page. ``text`` is the trimmed head+tail
    snippet (or a VLM transcription when ``via_vlm``); ``quality_ok`` is the
    calibrated text-layer verdict for this page alone."""

    page: int  # 1-based
    text: str
    quality_ok: bool
    via_vlm: bool = False

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "snippet": self.text,
            "quality_ok": self.quality_ok,
            "via_vlm": self.via_vlm,
        }


@dataclass(frozen=True)
class SplitPiece:
    start_page: int  # 1-based, inclusive
    end_page: int  # inclusive
    title: str
    doc_type: str
    confidence: float

    def to_dict(self) -> dict:
        return {
            "start_page": self.start_page,
            "end_page": self.end_page,
            "title": self.title,
            "doc_type": self.doc_type,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class SplitVerdict:
    """Outcome of detection. ``single`` → proceed with normal ingest (the
    common case and every fallback); ``multi`` → ``pieces`` holds ≥2 validated
    boundary pieces; ``needs_slow`` → route to the dedicated split lane
    (``slow_reason``: vlm | windows)."""

    kind: str
    pieces: list[SplitPiece] = field(default_factory=list)
    page_signals: list[PageSignal] = field(default_factory=list)
    slow_reason: str = ""

    @property
    def min_confidence(self) -> float:
        return min((p.confidence for p in self.pieces), default=0.0)


def single_verdict(signals: list[PageSignal] | None = None) -> SplitVerdict:
    return SplitVerdict(kind=VERDICT_SINGLE, page_signals=signals or [])


# ---------------------------------------------------------------------------
# Stage 1 — per-page text signals (blocking; callers run_in_executor)
# ---------------------------------------------------------------------------

def _squash_ws(text: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", text or "").strip()


def _snippet(text: str) -> str:
    """Head+tail snippet keeping the boundary-bearing regions of a page."""
    text = _squash_ws(text)
    if len(text) <= _SIGNAL_HEAD_CHARS + _SIGNAL_TAIL_CHARS:
        return text
    return (
        text[:_SIGNAL_HEAD_CHARS].rstrip()
        + " … "
        + text[-_SIGNAL_TAIL_CHARS:].lstrip()
    )


def extract_page_signals(file_path: str) -> list[PageSignal]:
    """Per-page text signals via the pypdfium2 text layer. Blocking (pdfium) —
    call via ``run_in_executor``. Returns ``[]`` on any failure (caller then
    treats the file as a single document)."""
    from services.document_processor import DocumentProcessor

    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(file_path)
        try:
            signals: list[PageSignal] = []
            for i in range(len(pdf)):
                try:
                    textpage = pdf[i].get_textpage()
                    # pypdfium2 4.x: get_text_bounded() (full page by default);
                    # older releases only have get_text_range().
                    if hasattr(textpage, "get_text_bounded"):
                        raw = textpage.get_text_bounded() or ""
                    else:  # pragma: no cover - legacy pypdfium2
                        raw = textpage.get_text_range() or ""
                except Exception:  # noqa: BLE001 - one broken page ≠ broken file
                    raw = ""
                usable, _reason = DocumentProcessor.assess_text_layer_quality(
                    raw, page_count=1
                )
                signals.append(
                    PageSignal(
                        page=i + 1,
                        text=_snippet(raw) if usable else _PLACEHOLDER_UNREADABLE,
                        quality_ok=usable,
                    )
                )
            return signals
        finally:
            pdf.close()
    except Exception as e:  # noqa: BLE001 - detection must never break ingest
        logger.warning(f"pdf-split: page-signal extraction failed for {file_path}: {e}")
        return []


# ---------------------------------------------------------------------------
# Stage 2 — slow-lane classification
# ---------------------------------------------------------------------------

def _signal_line(sig: PageSignal) -> str:
    return f"Seite {sig.page}: {sig.text}"


def classify_slow_lane(signals: list[PageSignal]) -> str | None:
    """Return the slow-lane reason for this file, or None when the inline
    (document-worker) path may decide directly: usable text layer AND all
    signals fit one boundary-LLM window."""
    if not signals:
        return None  # nothing to analyze — inline path yields single-doc
    garbage = sum(1 for s in signals if not s.quality_ok)
    if not signals[0].quality_ok or (garbage / len(signals)) > _GARBAGE_FRACTION_SLOW:
        return SLOW_REASON_VLM
    total_chars = sum(len(_signal_line(s)) + 1 for s in signals)
    if total_chars > settings.pdf_split_window_chars:
        return SLOW_REASON_WINDOWS
    return None


# ---------------------------------------------------------------------------
# Stage 4 — validation (pure, unit-tested hard)
# ---------------------------------------------------------------------------

_MAX_TITLE = 200
_MAX_DOC_TYPE = 60


def _clean(value: Any, cap: int) -> str:
    return (str(value).strip())[:cap] if value is not None else ""


def validate_boundaries(
    raw: dict | None, start_page: int, end_page: int
) -> list[SplitPiece] | None:
    """Coerce a raw LLM boundary payload into validated pieces covering
    ``start_page..end_page`` exactly: contiguous, non-overlapping, in order.
    Returns None when the payload cannot be salvaged (caller falls back to a
    single-document verdict)."""
    if not isinstance(raw, dict):
        return None
    docs = raw.get("documents")
    if not isinstance(docs, list) or not docs:
        return None

    pieces: list[SplitPiece] = []
    expected_start = start_page
    for entry in docs:
        if not isinstance(entry, dict):
            return None
        try:
            s = int(entry.get("start_page"))
            e = int(entry.get("end_page"))
        except (TypeError, ValueError):
            return None
        if s != expected_start or e < s or e > end_page:
            return None
        try:
            conf = float(entry.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = min(1.0, max(0.0, conf))
        pieces.append(
            SplitPiece(
                start_page=s,
                end_page=e,
                title=_clean(entry.get("title"), _MAX_TITLE),
                doc_type=_clean(entry.get("doc_type"), _MAX_DOC_TYPE),
                confidence=conf,
            )
        )
        expected_start = e + 1
    if expected_start != end_page + 1:
        return None  # coverage gap at the tail
    return pieces


# ---------------------------------------------------------------------------
# Stage 3 — boundary LLM call(s) with window batching
# ---------------------------------------------------------------------------

def _parse_llm_json(raw: str) -> dict | None:
    """Best-effort JSON out of an LLM reply (tolerates ```json fences / prose).
    Same local-copy convention as meeting_minutes / schicht_a_extractor."""
    if not raw:
        return None
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    candidate = fence.group(1) if fence else raw
    m = re.search(r"\{.*\}", candidate, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


def _build_windows(signals: list[PageSignal]) -> list[list[PageSignal]]:
    """Batch page signals into page-contiguous windows under the configured
    character budget. Purely a context-batching mechanism: every page appears
    in exactly one window, and a window always contains at least one page even
    if that page alone exceeds the budget (snippets are capped, so this is a
    degenerate safety case, not an expected one)."""
    budget = settings.pdf_split_window_chars
    windows: list[list[PageSignal]] = []
    current: list[PageSignal] = []
    used = 0
    for sig in signals:
        cost = len(_signal_line(sig)) + 1
        if current and used + cost > budget:
            windows.append(current)
            current, used = [], 0
        current.append(sig)
        used += cost
    if current:
        windows.append(current)
    return windows


async def _boundary_call(
    lines: list[str],
    *,
    start_page: int,
    end_page: int,
    carry_start: int | None,
    llm_client: Any,
    lang: str,
) -> dict | None:
    """One strict-JSON boundary call over the given signal lines."""
    model = settings.ollama_chat_model or settings.ollama_model
    if not model:
        logger.warning("pdf-split: no chat model configured")
        return None

    system = prompt_manager.get(
        "pdf_split", "system",
        default=(
            "Du analysierst die Seiten einer PDF-Datei, die mehrere unabhängige "
            "Einzeldokumente enthalten KANN (Stapelscan). Die Datei kann eine "
            "beliebige Mischung sein: viele einseitige Dokumente hintereinander, "
            "EIN langes mehrseitiges Dokument (z.B. ein Vertrag), oder beides "
            "gemischt. Erkenne Dokumentgrenzen NUR an inhaltlichen Belegen: "
            "neuer Briefkopf/Absender, neues Datum, neuer Betreff, "
            "'Seite 1 von N'-Reset, Grußformel/Unterschrift gefolgt von einem "
            "neuen Kopf. Niemals anhand von Längenannahmen. Anlagen, AGB und "
            "Anhänge gehören zu ihrem Hauptdokument. Im Zweifel WENIGER, "
            "größere Dokumente. Antworte AUSSCHLIESSLICH als JSON: "
            '{"documents": [{"start_page": 1, "end_page": 3, "title": "...", '
            '"doc_type": "...", "confidence": 0.0}]}. '
            "confidence ∈ [0,1] pro Dokument. Die Bereiche müssen lückenlos, "
            "überlappungsfrei und aufsteigend den gesamten angegebenen "
            "Seitenbereich abdecken."
        ),
        lang=lang,
    )
    carry_note = ""
    if carry_start is not None and carry_start < start_page:
        carry_note = (
            f"Hinweis: Auf Seite {carry_start} beginnt ein Dokument, das sich "
            f"in diesen Ausschnitt fortsetzen kann. Das erste Dokument deiner "
            f"Antwort MUSS auf Seite {carry_start} beginnen.\n"
        )
    user = prompt_manager.get(
        "pdf_split", "user",
        default=(
            "{carry_note}Seitenbereich: {range_start} bis {range_end}.\n"
            "Seiteninhalte (Anfang … Ende jeder Seite):\n\n{pages}"
        ),
        lang=lang,
        carry_note=carry_note,
        range_start=carry_start if carry_start is not None else start_page,
        range_end=end_page,
        pages="\n".join(lines),
    )

    client = llm_client or get_default_client()
    response = await client.chat(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        options={"temperature": 0.1},
        **get_classification_chat_kwargs(model),
    )
    return _parse_llm_json(extract_response_content(response) or "")


async def detect_boundaries(
    signals: list[PageSignal],
    *,
    llm_client: Any = None,
    lang: str = "de",
) -> SplitVerdict:
    """Boundary detection over prepared page signals. Handles arbitrary length
    via windowing with an open-trailing-piece carry: a non-final window's last
    piece may continue past the window edge, so it is re-decided by the next
    window (which is told the open document's start page). Never raises;
    anything unparseable/invalid collapses to a single-document verdict."""
    if not signals:
        return single_verdict(signals)
    last_page = signals[-1].page

    try:
        windows = _build_windows(signals)
        pieces: list[SplitPiece] = []
        carry_start: int | None = None
        for w_idx, window in enumerate(windows):
            w_start, w_end = window[0].page, window[-1].page
            is_final = w_idx == len(windows) - 1
            raw = await _boundary_call(
                [_signal_line(s) for s in window],
                start_page=w_start,
                end_page=w_end,
                carry_start=carry_start,
                llm_client=llm_client,
                lang=lang,
            )
            effective_start = (
                carry_start if carry_start is not None and carry_start < w_start
                else w_start
            )
            window_pieces = validate_boundaries(raw, effective_start, w_end)
            if window_pieces is None:
                logger.info(
                    f"pdf-split: unusable boundary response for pages "
                    f"{effective_start}-{w_end} — single-document verdict"
                )
                return single_verdict(signals)
            if is_final:
                pieces.extend(window_pieces)
            else:
                # The last piece may continue past the window edge; the next
                # window re-decides it from its start page.
                pieces.extend(window_pieces[:-1])
                carry_start = window_pieces[-1].start_page

        final = validate_boundaries(
            {"documents": [p.to_dict() for p in pieces]}, signals[0].page, last_page
        )
        if final is None or len(final) < 2:
            return single_verdict(signals)
        return SplitVerdict(kind=VERDICT_MULTI, pieces=final, page_signals=signals)
    except Exception as e:  # noqa: BLE001 - detection must never break ingest
        logger.warning(f"pdf-split: boundary detection failed: {e}")
        return single_verdict(signals)
