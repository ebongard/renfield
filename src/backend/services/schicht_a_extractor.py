"""Schicht A field extractor — structured facts from a document's ``field_text``.

Runs as the ``post_document_ingest`` consumer (see
``schicht_a_post_document_ingest_hook``). Hybrid by design:

  * **Deterministic layer** — regex over a *whitespace-normalized* view of the
    text for the two identifiers whose format is fixed AND whose recovery the
    normalization step gates: **Steuernummer** and **IBAN**. poppler
    ``pdftotext -layout`` letter-spaces wide-tracked lines, so a Steuernummer
    arrives as ``11 4 / 5 8 7 6 / 5 2 9 3`` and the keyword as
    ``S t e u e r n u m m e r``. We collapse intra-number spacing for value
    extraction and fully squish for keyword-presence gating. Deterministic =
    high trust, no LLM cost, the ``normalized_value`` is exact-matchable.

  * **LLM layer** — obligations (zahlung/kuendigung/widerspruch/… with a
    date/amount/legal_gate, recall being the safety axis) plus universal facts
    (issuer, total, Rechnungsnummer). Mirrors ``PaperlessMetadataExtractor``:
    classification kwargs + thinking-model-safe ``extract_response_content``.

The extractor is pure (text in, typed facts out). Storage as atoms lives in the
hook. Spec: ``tests/eval/schicht_a_fixtures_local/labels.yaml`` (local/gitignored).
"""
from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from models.database import (
    DOC_FACT_CATEGORY_IDENTIFIER,
    DOC_FACT_CATEGORY_OBLIGATION,
    DOC_FACT_CATEGORY_UNIVERSAL,
    DOC_FACT_SOURCE_DETERMINISTIC,
    DOC_FACT_SOURCE_LLM,
)
from services.prompt_manager import prompt_manager
from utils.config import settings
from utils.llm_client import (
    extract_response_content,
    get_classification_chat_kwargs,
    get_default_client,
)

# Obligation kinds that are ALWAYS human-confirmed regardless of what the LLM
# says about legal_gate (golden set: widerspruch/statutory => mandatory review).
_ALWAYS_LEGAL_GATE_KINDS = {"widerspruch"}

_MAX_DOC_CHARS = 12_000  # prompt budget; field_text beyond this is truncated
_MAX_OBLIGATIONS = 50    # hard cap on LLM obligations (hostile/garbage JSON guard)
_MAX_FACTS_PER_DOC = 100  # hard cap on facts stored per document (write-amplification guard)

# Steuernummer: German tax number, NN(N)/NNN(N)/NNNN(N). Distinctive enough that
# a keyword gate (below) is the precision guard, not the pattern alone.
_STEUERNUMMER_RE = re.compile(r"\b(\d{2,3}/\d{3,4}/\d{4,5})\b")
# German IBAN: DE + 2 check digits + 18 digits = 22 chars, no internal spaces
# after full-collapse. DE\d{20} is specific enough that a spurious 20-digit run
# is vanishingly rare.
_IBAN_DE_RE = re.compile(r"\b(DE\d{20})\b")


class ExtractedFact(BaseModel):
    """One fact, shaped to the ``document_facts`` row it becomes."""
    category: str                         # identifier | obligation | universal
    kind: str                             # steuernummer | zahlung | issuer | ...
    value: str                            # verbatim value / short summary
    normalized_value: str | None = None   # whitespace-collapsed (identifiers)
    excerpt: str | None = None            # verbatim source span (trust anchor)
    obligation_date: date | None = None
    amount_value: Decimal | None = None
    amount_currency: str | None = None
    legal_gate: bool = False
    payment_method: str | None = None
    confidence: float | None = None
    source: str = DOC_FACT_SOURCE_DETERMINISTIC


class SchichtAResult(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Normalization — the load-bearing step
# ---------------------------------------------------------------------------


def normalize_field_text(text: str) -> str:
    """Collapse poppler ``-layout`` intra-number letter-spacing.

    ``11 4 / 5 8 7 6 / 5 2 9 3`` -> ``114/5876/5293``. Only collapses a run of
    whitespace when BOTH neighbours are a digit or ``/`` — so prose words and
    normal spacing are untouched; only spaced-out numeric identifiers close up.
    """
    if not text:
        return ""
    return re.sub(r"(?<=[\d/])\s+(?=[\d/])", "", text)


def _squish(text: str) -> str:
    """Strip every non-alphanumeric and lowercase — a keyword-presence view that
    survives arbitrary letter-spacing (``S t e u e r n u m m e r`` -> contains
    ``steuernummer``). Boolean gate only; never used for value extraction."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


# ---------------------------------------------------------------------------
# Deterministic identifier extraction
# ---------------------------------------------------------------------------


def extract_identifiers(field_text: str) -> list[ExtractedFact]:
    """Steuernummer + IBAN from a whitespace-normalized view. Keyword-gated for
    precision (recall is the safety axis, but a bare NN/NNNN/NNNN run with no
    'steuernummer' anywhere in the doc is more likely a coincidence than a tax
    number). De-duplicated by normalized value."""
    if not field_text:
        return []
    norm = normalize_field_text(field_text)
    facts: list[ExtractedFact] = []
    seen: set[tuple[str, str]] = set()

    for m in _STEUERNUMMER_RE.finditer(norm):
        # Co-location gate: the keyword must sit NEAR this number, not merely
        # somewhere in the document. A whole-doc "steuernummer" check turns any
        # slash-grouped run (date ranges, article/order numbers) on an invoice
        # that merely mentions the word in a footer into a false fact. Squish a
        # local window so a letter-spaced keyword ("S t e u e r n u m m e r")
        # still matches.
        window = _squish(norm[max(0, m.start() - 120):m.end() + 20])
        if "steuernummer" not in window and "stnr" not in window:
            continue
        val = m.group(1)
        key = ("steuernummer", val)
        if key in seen:
            continue
        seen.add(key)
        facts.append(ExtractedFact(
            category=DOC_FACT_CATEGORY_IDENTIFIER,
            kind="steuernummer",
            value=val,
            normalized_value=val,
            excerpt=_window(norm, m.start(), m.end()),
            confidence=0.95,
            source=DOC_FACT_SOURCE_DETERMINISTIC,
        ))

    # IBAN from the SAME intra-number-normalized view: group-spacing
    # ("DE89 3704 0044 …") closes up to "DE89370400440532013000", while word
    # spacing is preserved so the trailing \b still holds (a full collapse would
    # merge the IBAN with the next word and lose the boundary).
    for m in _IBAN_DE_RE.finditer(norm):
        val = m.group(1)
        key = ("iban", val)
        if key in seen:
            continue
        seen.add(key)
        facts.append(ExtractedFact(
            category=DOC_FACT_CATEGORY_IDENTIFIER,
            kind="iban",
            value=val,
            normalized_value=val,
            excerpt=val,
            confidence=0.97,
            source=DOC_FACT_SOURCE_DETERMINISTIC,
        ))

    return facts


def _window(text: str, start: int, end: int, pad: int = 40) -> str:
    """A short verbatim span around a match, for the trust anchor."""
    return text[max(0, start - pad):min(len(text), end + pad)].strip()


# ---------------------------------------------------------------------------
# LLM obligation + universal-fact extraction
# ---------------------------------------------------------------------------


class SchichtAExtractor:
    """Hybrid extractor. One per request; ``llm_client`` injectable for tests."""

    def __init__(self, llm_client: Any = None):
        self._llm_client = llm_client

    async def extract(self, field_text: str, *, lang: str = "de") -> SchichtAResult:
        """Deterministic identifiers + LLM obligations/universal facts, merged
        and de-duplicated. Never raises — extraction failure returns whatever the
        deterministic layer found plus ``error`` set."""
        if not field_text or not field_text.strip():
            return SchichtAResult(facts=[])

        facts = extract_identifiers(field_text)
        seen_ident = {(f.kind, f.normalized_value or f.value) for f in facts}

        error: str | None = None
        try:
            llm_facts = await self._extract_llm(field_text, lang=lang)
            for f in llm_facts:
                # Don't let the LLM duplicate an identifier the deterministic
                # layer already nailed (its normalized form is authoritative).
                # Compare on the SAME key the dedup set uses — the LLM's raw value
                # is usually space-grouped ("DE89 3704 …"), so keying on f.value
                # would miss the dupe; key on its normalized_value.
                if f.category == DOC_FACT_CATEGORY_IDENTIFIER and (
                    f.kind, f.normalized_value or f.value
                ) in seen_ident:
                    continue
                facts.append(f)
        except Exception as e:  # noqa: BLE001 — extraction is best-effort
            logger.warning(f"Schicht A LLM extraction failed: {e}")
            error = "llm_extraction_failed"

        return SchichtAResult(facts=facts, error=error)

    async def _extract_llm(self, field_text: str, *, lang: str) -> list[ExtractedFact]:
        model = (
            settings.schicht_a_extraction_model
            or settings.ollama_chat_model
            or settings.ollama_model
        )
        if not model:
            raise RuntimeError("No extraction model configured")

        system = prompt_manager.get(
            "schicht_a_extraction", "system",
            default="Extract obligations and facts as JSON.", lang=lang,
        )
        user = prompt_manager.get(
            "schicht_a_extraction", "user",
            default="{document_text}", lang=lang,
            document_text=field_text[:_MAX_DOC_CHARS],
        )

        client = self._llm_client or get_default_client()
        classification_kwargs = get_classification_chat_kwargs(model)
        response = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            options={"temperature": 0.1, "num_predict": 1200},
            **classification_kwargs,
        )
        raw = extract_response_content(response) or ""
        payload = _parse_llm_json(raw)
        if payload is None:
            raise ValueError("unparseable LLM response")
        return _facts_from_payload(payload)


# ---------------------------------------------------------------------------
# LLM payload → facts
# ---------------------------------------------------------------------------


def _facts_from_payload(payload: dict) -> list[ExtractedFact]:
    """Map the LLM JSON ({obligations:[...], universal_facts:{...}}) to facts.
    Tolerant of missing/oddly-typed fields — a malformed entry is skipped, not
    fatal (recall: keep the good ones)."""
    facts: list[ExtractedFact] = []

    for ob in (payload.get("obligations") or [])[:_MAX_OBLIGATIONS]:
        if not isinstance(ob, dict):
            continue
        # Bound to the kind column width (String(32)). An unbounded value would
        # raise StringDataRightTruncation on Postgres flush (sqlite silently
        # ignores it), which the hook's outer except would swallow — losing the
        # whole batch. .lower() AFTER truncation.
        kind = (_clean_str(ob.get("kind"), 32) or "").lower()
        if not kind:
            continue
        amount = ob.get("amount") if isinstance(ob.get("amount"), dict) else {}
        legal_gate = bool(ob.get("legal_gate")) or kind in _ALWAYS_LEGAL_GATE_KINDS
        facts.append(ExtractedFact(
            category=DOC_FACT_CATEGORY_OBLIGATION,
            kind=kind,
            value=str(ob.get("excerpt") or kind)[:500],
            excerpt=str(ob.get("excerpt") or "")[:500] or None,
            obligation_date=_parse_date(ob.get("date")),
            amount_value=_parse_amount(amount.get("value")),
            amount_currency=_clean_currency(amount.get("currency")),
            legal_gate=legal_gate,
            payment_method=_clean_str(ob.get("payment_method"), 16),
            confidence=0.7,
            source=DOC_FACT_SOURCE_LLM,
        ))

    uni = payload.get("universal_facts")
    if isinstance(uni, dict):
        issuer = _clean_str(uni.get("issuer"), 500)
        if issuer:
            facts.append(ExtractedFact(
                category=DOC_FACT_CATEGORY_UNIVERSAL, kind="issuer",
                value=issuer, confidence=0.7, source=DOC_FACT_SOURCE_LLM,
            ))
        total = uni.get("total") if isinstance(uni.get("total"), dict) else None
        if total:
            amt = _parse_amount(total.get("value"))
            if amt is not None:
                facts.append(ExtractedFact(
                    category=DOC_FACT_CATEGORY_UNIVERSAL, kind="total",
                    value=str(total.get("value")),
                    amount_value=amt,
                    amount_currency=_clean_currency(total.get("currency")),
                    confidence=0.7, source=DOC_FACT_SOURCE_LLM,
                ))
        for ident in uni.get("identifiers") or []:
            if not isinstance(ident, dict):
                continue
            ikind = _clean_str(ident.get("kind"), 32)
            ival = _clean_str(ident.get("value"), 500)
            if ikind and ival:
                facts.append(ExtractedFact(
                    category=DOC_FACT_CATEGORY_IDENTIFIER, kind=ikind.lower(),
                    value=ival, normalized_value=re.sub(r"\s+", "", ival),
                    confidence=0.6, source=DOC_FACT_SOURCE_LLM,
                ))

    return facts


def _parse_llm_json(raw: str) -> dict | None:
    """Parse an LLM response to a dict; tolerate markdown fences + surrounding
    prose. Returns None if nothing parseable (caller treats as failure)."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    first, last = text.find("{"), text.rfind("}")
    if first < 0 or last <= first:
        return None
    try:
        parsed = json.loads(text[first:last + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_date(value: Any) -> date | None:
    """ISO yyyy-mm-dd or dd.mm.yyyy → date. AS PRINTED; never computed."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", v)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", v)
    if m:
        try:
            return date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            return None
    return None


def _parse_amount(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return Decimal(str(value))
        s = str(value).strip().replace(" ", "")
        # German "1.234,56" → "1234.56"; plain "107.10" passes through.
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        s = re.sub(r"[^\d.\-]", "", s)
        return Decimal(s) if s else None
    except (InvalidOperation, ValueError):
        return None


def _clean_str(value: Any, maxlen: int) -> str | None:
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    return s[:maxlen] if s else None


def _clean_currency(value: Any) -> str | None:
    c = _clean_str(value, 8)
    return c.upper() if c else None


# ---------------------------------------------------------------------------
# post_document_ingest hook — extract + store facts as atoms
# ---------------------------------------------------------------------------


async def schicht_a_post_document_ingest_hook(
    chunks: list[str],
    document_id: int | None = None,
    user_id: int | None = None,
    field_text: str = "",
    lang: str | None = None,
    **kwargs: Any,
) -> None:
    """Extract Schicht A facts from ``field_text`` and store them as atoms.

    Registered on ``post_document_ingest`` (api/lifecycle.py). Opt-in via
    ``settings.schicht_a_extraction_enabled``. Best-effort: any failure logs a
    warning and returns — ingestion has already committed the chunks, so a fact
    extraction miss never fails the upload. Idempotent on re-ingest: prior facts
    for the document are purged (via the sanctioned AtomPurgeService) before
    re-extraction, so reindexing a document refreshes its facts instead of
    duplicating them.
    """
    if not settings.schicht_a_extraction_enabled:
        return
    if document_id is None or not (field_text or "").strip():
        return

    from sqlalchemy import select

    from models.database import ATOM_TYPE_DOCUMENT_FACT, Atom, Document, DocumentFact
    from services.atom_purge_service import AtomPurgeService
    from services.atom_service import AtomService
    from services.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            doc = (await db.execute(
                select(Document).where(Document.id == document_id)
            )).scalar_one_or_none()
            if doc is None:
                return

            # Owner = the document's own atom owner (authoritative); fall back to
            # the ingesting user. Without an owner we can't mint atoms — skip.
            owner_id: int | None = user_id
            if doc.atom_id:
                doc_atom = (await db.execute(
                    select(Atom).where(Atom.atom_id == doc.atom_id)
                )).scalar_one_or_none()
                if doc_atom is not None:
                    owner_id = doc_atom.owner_user_id
            if owner_id is None:
                logger.warning(
                    f"Schicht A: no owner for doc {document_id}; skipping fact storage"
                )
                return

            tier = int(doc.circle_tier or 0)
            lang = lang or settings.default_language

            result = await SchichtAExtractor().extract(field_text, lang=lang)
            if not result.facts:
                return

            # Capture prior fact-atoms BEFORE writing — they're purged only AFTER
            # the new set is safely committed. AtomPurgeService.purge commits per
            # call, so a purge-then-write ordering would, on any write failure,
            # leave the document with ZERO facts. Write-new-then-purge-old means
            # the worst case is recoverable duplicates (cleaned on the next
            # reindex), never silent loss of a document's facts.
            old_atom_ids = (await db.execute(
                select(DocumentFact.atom_id).where(
                    DocumentFact.document_id == document_id
                )
            )).scalars().all()

            atom_svc = AtomService(db)
            stored = 0
            capped = result.facts[:_MAX_FACTS_PER_DOC]
            if len(result.facts) > _MAX_FACTS_PER_DOC:
                logger.warning(
                    f"Schicht A: doc {document_id} produced {len(result.facts)} "
                    f"facts; capping at {_MAX_FACTS_PER_DOC}"
                )
            for f in capped:
                atom_id = await atom_svc.create_with_source(
                    atom_type=ATOM_TYPE_DOCUMENT_FACT,
                    owner_user_id=int(owner_id),
                    tier=tier,
                )
                row = DocumentFact(
                    document_id=document_id,
                    category=f.category,
                    kind=f.kind,
                    value=f.value,
                    normalized_value=f.normalized_value,
                    excerpt=f.excerpt,
                    obligation_date=f.obligation_date,
                    amount_value=f.amount_value,
                    amount_currency=f.amount_currency,
                    legal_gate=f.legal_gate,
                    payment_method=f.payment_method,
                    confidence=f.confidence,
                    source=f.source,
                    atom_id=atom_id,
                    circle_tier=tier,
                )
                db.add(row)
                await db.flush()
                await atom_svc.finalize_source_id(atom_id, row.id)
                stored += 1

            await db.commit()

            # New facts are committed; now retire the prior set.
            for aid in old_atom_ids:
                if aid:
                    await AtomPurgeService.purge(
                        db, atom_id=aid, reason="schicht_a_reextract"
                    )

            logger.info(
                f"Schicht A: stored {stored} fact(s) for doc {document_id} "
                f"(tier={tier})"
            )
    except Exception as e:  # noqa: BLE001 — never fail the ingest on a fact miss
        logger.warning(f"Schicht A post_document_ingest hook failed: {e}")
