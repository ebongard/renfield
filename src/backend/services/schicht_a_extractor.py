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

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

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

# Obligations that are ALWAYS human-confirmed regardless of what the LLM says
# about legal_gate (golden set: statutory remedy => mandatory review). Now that
# the obligation kind is an OPEN label (no fixed enum), match keywords on the
# kind OR the excerpt — an exact-kind set would miss e.g. a "termin" whose
# excerpt cites a Widerspruch. Recall-first: over-gating only adds a review
# step, a missed statutory deadline is the real harm.
_LEGAL_GATE_KEYWORDS = (
    "widerspruch", "einspruch", "klage", "rechtsbehelf",
    "berufung", "beschwerde", "revision",
)
# Left-boundary (prefix) match, NOT \bword\b: German legal terms compound
# ("Widerspruchsfrist", "Klageschrift"), so the right edge must stay open to keep
# recall. The left \b kills the substring false positives where the keyword is a
# suffix/infix ("Anklage", "Beklagte", "Preisrevision", "Verklagen").
_LEGAL_GATE_RE = re.compile(r"\b(?:" + "|".join(_LEGAL_GATE_KEYWORDS) + r")")

_MAX_DOC_CHARS = 12_000  # prompt budget; field_text beyond this is truncated
_MAX_OBLIGATIONS = 50    # hard cap on LLM obligations (hostile/garbage JSON guard)
_MAX_OPEN_FACTS = 60     # hard cap on open LLM key-facts per source (write-amplification guard)
# Per-doc total cap. Keep >= _MAX_OBLIGATIONS + _MAX_OPEN_FACTS so the store-side
# slice can't silently drop already-extracted facts from a legitimately rich doc.
_MAX_FACTS_PER_DOC = 120


def _as_list(value: object) -> list:
    """Coerce untrusted LLM JSON to a list. A truthy non-list (dict/int/str)
    would otherwise survive ``x or []`` and reach a slice/iteration that raises
    out of _facts_from_payload — discarding the whole batch. Anything that isn't
    a list becomes []."""
    return value if isinstance(value, list) else []


def _is_legal_gate(kind: str, excerpt: object) -> bool:
    """A statutory-remedy deadline (Widerspruch/Einspruch/Klage…) always needs
    human confirmation. Keyword-matched across the kind AND the excerpt so an
    open/free kind label can't slip a legal deadline past the gate."""
    hay = f"{kind} {excerpt or ''}".lower()
    return _LEGAL_GATE_RE.search(hay) is not None

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


def _fact_identity_key(
    category: object, kind: object, normalized_value: object, value: object
) -> tuple[str, str, str]:
    """Stable cross-re-extraction identity for a fact row.

    A re-extraction recreates the whole ``document_facts`` set from scratch
    (new ids, new atoms), so a per-fact tier override (``tier_overridden``)
    can only be carried forward by matching a freshly-extracted fact to its
    prior version on *content* identity, not row id.

    Identity = ``(category, kind, value-signature)`` where the value signature
    prefers ``normalized_value`` (identifiers — the whitespace-collapsed form
    is the stable anchor) and falls back to ``value`` (obligations / universals
    whose value is a short summary). Both are ``_squish``-ed so arbitrary
    poppler letter-spacing / case differences between two OCR passes don't
    break the match. Deterministic regex facts (identifiers) match reliably;
    LLM summaries may drift between passes — a drifted summary simply doesn't
    match and the re-extracted fact reverts to the doc tier (the documented
    fail-safe: never MORE visible than the parent doc).
    """
    sig_src = normalized_value if (normalized_value not in (None, "")) else value
    return (
        str(category or "").lower(),
        str(kind or "").lower(),
        _squish(str(sig_src or "")),
    )


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
        # No num_predict by default (settings value 0) → let the model generate
        # to completion (bounded by the server context). A fixed cap truncated
        # rich docs → unparseable JSON → lost facts. Set >0 only to bound a
        # misbehaving model.
        options: dict[str, Any] = {"temperature": 0.1}
        if settings.schicht_a_extraction_num_predict > 0:
            options["num_predict"] = settings.schicht_a_extraction_num_predict
        response = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            options=options,
            **classification_kwargs,
        )
        raw = extract_response_content(response) or ""
        payload = _parse_llm_json(raw)
        if payload is None:
            raise ValueError("unparseable LLM response")
        return _facts_from_payload(payload)


# ---------------------------------------------------------------------------
# Document title synthesis (from facts, for the Wissen/Dokumente display name)
# ---------------------------------------------------------------------------

_MAX_TITLE_LEN = 160

_TITLE_SYSTEM_DEFAULT = (
    "Du erzeugst aus den extrahierten Fakten eines Dokuments einen kurzen, "
    "sprechenden Titel für eine Dokumentenliste. Nimm den Aussteller/Absender, "
    "die Dokumentart und (falls vorhanden) das Datum. Leite die Dokumentart aus "
    "dem Kontext ab (z. B. Rechnung, Mahnung, Bescheid, Vertrag, Kündigung, "
    "Fragebogen, Pfändung), auch wenn sie nicht wörtlich als Fakt vorliegt. "
    "Maximal ~80 Zeichen, keine Anführungszeichen im Titel, kein Dateiname. "
    "Antworte AUSSCHLIESSLICH als JSON: {\"title\": \"<Titel>\"}."
)
_TITLE_USER_DEFAULT = "Fakten des Dokuments:\n{facts}"


def _facts_to_block(facts: list[Any]) -> str:
    """Compact 'kind: value (Frist …) (Betrag …)' lines for the title prompt.
    Accepts ExtractedFact or DocumentFact rows (duck-typed on the shared attrs),
    so the same synthesizer serves ingest and the backfill."""
    lines: list[str] = []
    for f in (facts or [])[:_MAX_FACTS_PER_DOC]:
        kind = getattr(f, "kind", "") or ""
        value = (getattr(f, "value", "") or "")[:120]
        extra = ""
        od = getattr(f, "obligation_date", None)
        if od:
            extra += f" (Frist {od})"
        av = getattr(f, "amount_value", None)
        if av is not None:
            cur = getattr(f, "amount_currency", "") or ""
            extra += f" (Betrag {av} {cur})" if cur else f" (Betrag {av})"
        lines.append(f"- {kind}: {value}{extra}")
    return "\n".join(lines)


async def generate_document_title(
    facts: list[Any], *, lang: str = "de", llm_client: Any = None
) -> str | None:
    """Synthesize a short human-readable title from a document's Schicht A facts
    (issuer + document type + date, inferring the type from context).

    Works from the FACTS, never the raw OCR text — so the ingest hook and the
    one-off backfill produce identical titles. Returns None on no facts / no model
    / generation failure (caller keeps the metadata title or filename). Never
    raises — title synthesis must not break ingest.
    """
    if not facts:
        return None
    model = (
        settings.schicht_a_extraction_model
        or settings.ollama_chat_model
        or settings.ollama_model
    )
    if not model:
        return None

    system = prompt_manager.get(
        "schicht_a_title", "system", default=_TITLE_SYSTEM_DEFAULT, lang=lang,
    )
    # Substitute {facts} ourselves (not via prompt_manager kwargs): prompt_manager
    # returns the DEFAULT verbatim when the key isn't in the YAML, so a kwarg would
    # never reach the default template. .replace (not .format) is brace-safe — fact
    # values can contain literal braces.
    user_template = prompt_manager.get(
        "schicht_a_title", "user", default=_TITLE_USER_DEFAULT, lang=lang,
    )
    facts_block = _facts_to_block(facts)
    user = (
        user_template.replace("{facts}", facts_block)
        if "{facts}" in user_template
        else f"{user_template}\n{facts_block}"
    )
    try:
        client = llm_client or get_default_client()
        classification_kwargs = get_classification_chat_kwargs(model)
        response = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            options={"temperature": 0.2},
            **classification_kwargs,
        )
        raw = extract_response_content(response) or ""
        payload = _parse_llm_json(raw)
        if not isinstance(payload, dict):
            return None
        title = payload.get("title")
        if isinstance(title, str):
            title = " ".join(title.split()).strip().strip('"').strip()[:_MAX_TITLE_LEN]
            return title or None
    except Exception as e:  # noqa: BLE001 — title synthesis never breaks ingest
        logger.warning(f"Schicht A title synthesis failed: {e}")
    return None


# ---------------------------------------------------------------------------
# LLM payload → facts
# ---------------------------------------------------------------------------


def _facts_from_payload(payload: dict) -> list[ExtractedFact]:
    """Map the LLM JSON to facts. Current prompt schema is
    {obligations:[...], facts:[{category,kind,value,amount?,excerpt}, ...]}; the
    legacy {universal_facts:{issuer,total,identifiers}} shape is still mapped as a
    safety net in case the model emits the older shape (the prompt no longer asks
    for it). Tolerant of missing/oddly-typed fields — a malformed entry is skipped,
    not fatal (recall: keep the good ones)."""
    facts: list[ExtractedFact] = []

    for ob in _as_list(payload.get("obligations"))[:_MAX_OBLIGATIONS]:
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
        legal_gate = bool(ob.get("legal_gate")) or _is_legal_gate(kind, ob.get("excerpt"))
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
        for ident in _as_list(uni.get("identifiers")):
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

    # Open key-fact list (current prompt schema). Each entry is
    # {category, kind, value, amount?, excerpt} where the LLM's rich `category`
    # (party|date|amount|identifier|reference|status|other) is collapsed into
    # the two stored buckets: identifier/reference -> IDENTIFIER (carries a
    # whitespace-stripped normalized_value for matching), everything else ->
    # UNIVERSAL. `kind` is a free snake_case label; the deliberately open shape
    # lets each document surface its own Eckdaten instead of fixed slots.
    for f in _as_list(payload.get("facts"))[:_MAX_OPEN_FACTS]:
        if not isinstance(f, dict):
            continue
        # .lower() AFTER the 32-char truncation (kind column width — see the
        # obligation loop above for the StringDataRightTruncation rationale).
        kind = (_clean_str(f.get("kind"), 32) or "").lower()
        value = _clean_str(f.get("value"), 500)
        if not kind or not value:
            continue
        # _clean_str (not raw .strip()) so a non-str category from a hostile
        # payload can't raise AttributeError and discard the whole batch.
        is_ident = (_clean_str(f.get("category"), 16) or "").lower() in ("identifier", "reference")
        amount = f.get("amount") if isinstance(f.get("amount"), dict) else {}
        facts.append(ExtractedFact(
            category=DOC_FACT_CATEGORY_IDENTIFIER if is_ident else DOC_FACT_CATEGORY_UNIVERSAL,
            kind=kind,
            value=value,
            normalized_value=re.sub(r"\s+", "", value) if is_ident else None,
            excerpt=_clean_str(f.get("excerpt"), 500),
            amount_value=_parse_amount(amount.get("value")),
            amount_currency=_clean_currency(amount.get("currency")),
            confidence=0.7,
            source=DOC_FACT_SOURCE_LLM,
        ))

    return facts


def _parse_llm_json(raw: str) -> dict | None:
    """Thin delegate to the shared ``utils.llm_client.parse_llm_json`` — the
    fence-tolerant strict parse + truncation salvage (which once cost doc 43
    all 14 facts before the salvage existed) moved there so every strict-JSON
    extractor shares ONE hardened implementation. Kept as a local name so call
    sites/tests are unchanged; the shared helper logs when salvage kicks in."""
    from utils.llm_client import parse_llm_json

    return parse_llm_json(raw)


def _salvage_truncated_json(s: str) -> dict | None:
    """Thin delegate — implementation moved to utils.llm_client.salvage_truncated_json."""
    from utils.llm_client import salvage_truncated_json

    return salvage_truncated_json(s)


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


# ISO-4217 active alpha-3 currency codes. A static set (no runtime dependency):
# the LLM is asked for a currency but can hallucinate a free-form string, so we
# validate against the standard and drop anything that isn't a real code.
_ISO_4217: frozenset[str] = frozenset({
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN",
    "BAM", "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BOV",
    "BRL", "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHE", "CHF",
    "CHW", "CLF", "CLP", "CNY", "COP", "COU", "CRC", "CUC", "CUP", "CVE",
    "CZK", "DJF", "DKK", "DOP", "DZD", "EGP", "ERN", "ETB", "EUR", "FJD",
    "FKP", "GBP", "GEL", "GHS", "GIP", "GMD", "GNF", "GTQ", "GYD", "HKD",
    "HNL", "HTG", "HUF", "IDR", "ILS", "INR", "IQD", "IRR", "ISK", "JMD",
    "JOD", "JPY", "KES", "KGS", "KHR", "KMF", "KPW", "KRW", "KWD", "KYD",
    "KZT", "LAK", "LBP", "LKR", "LRD", "LSL", "LYD", "MAD", "MDL", "MGA",
    "MKD", "MMK", "MNT", "MOP", "MRU", "MUR", "MVR", "MWK", "MXN", "MXV",
    "MYR", "MZN", "NAD", "NGN", "NIO", "NOK", "NPR", "NZD", "OMR", "PAB",
    "PEN", "PGK", "PHP", "PKR", "PLN", "PYG", "QAR", "RON", "RSD", "RUB",
    "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD", "SHP", "SLE", "SOS",
    "SRD", "SSP", "STN", "SVC", "SYP", "SZL", "THB", "TJS", "TMT", "TND",
    "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX", "USD", "USN", "UYI",
    "UYU", "UYW", "UZS", "VED", "VES", "VND", "VUV", "WST", "XAF", "XAG",
    "XAU", "XCD", "XDR", "XOF", "XPF", "XSU", "XUA", "YER", "ZAR", "ZMW",
    "ZWG",
})

# Common symbols / words the LLM may emit instead of an ISO code, mapped to the
# canonical alpha-3. Applied before the ISO-4217 check.
_CURRENCY_ALIASES: dict[str, str] = {
    "€": "EUR", "EURO": "EUR", "EUROS": "EUR",
    "$": "USD", "US$": "USD", "USD$": "USD", "DOLLAR": "USD", "DOLLARS": "USD",
    "£": "GBP", "POUND": "GBP", "POUNDS": "GBP",
    "¥": "JPY", "FR": "CHF", "FR.": "CHF", "SFR": "CHF", "CHF.": "CHF",
}


def _clean_currency(value: Any) -> str | None:
    """Normalize a currency to a valid ISO-4217 alpha-3 code, else drop it.

    Maps common symbols/words (€, $, "Euro", …) to their code, then validates
    against ISO-4217. A hallucinated / non-currency string returns None so it
    never lands on a fact as bogus data (the field is nullable).
    """
    c = _clean_str(value, 8)
    if not c:
        return None
    up = c.upper()
    up = _CURRENCY_ALIASES.get(up, up)
    return up if up in _ISO_4217 else None


# ---------------------------------------------------------------------------
# post_document_ingest hook — extract + store facts as atoms
# ---------------------------------------------------------------------------

# Per-document advisory-lock namespace for Schicht A re-extraction. 0x5341 = "SA".
# Kept distinct from other subsystems' namespaces (e.g. the KG reconciler's
# _RECONCILER_LOCK_NS = 0x4B47) so their locks never collide.
_SCHICHT_A_REINDEX_LOCK_NS = 0x5341

# Bounded wait for the DEDICATED advisory-lock connection. The hook already holds
# a pooled session connection, and this lock opens a SECOND one; a folder-ingest
# backlog fanning many hooks at once could otherwise pile up on the pool (the
# failure class behind the 2026-07-01 watch-folder outage). A short timeout means
# that under pool pressure we DEGRADE to running unlocked (a rare duplicate the
# next reindex reconciles) rather than blocking the ingest path or exhausting the
# pool — breaking the hold-and-wait needed for a two-resource deadlock.
_LOCK_CONN_ACQUIRE_TIMEOUT_S = 5.0


def _resolve_lock_engine(bind: Any) -> AsyncEngine | None:
    """The AsyncEngine to open the dedicated advisory-lock connection on.

    Mirrors ``kg_reconciler_service._resolve_lock_engine`` (kept local to avoid
    coupling two feature modules over a 3-line helper). An ``AsyncSession``
    commit returns its connection to the pool — which drops a session-level
    advisory lock — so the lock must live on a SEPARATE connection that spans the
    hook's mid-flight commit + the post-commit purge. Prod binds to an
    ``AsyncEngine`` (use directly; its ``.engine`` is the SYNC engine); tests bind
    to an ``AsyncConnection`` (its ``.engine`` IS the AsyncEngine).
    """
    if isinstance(bind, AsyncEngine):
        return bind
    if isinstance(bind, AsyncConnection):
        return bind.engine
    return None


@asynccontextmanager
async def _reindex_lock(bind: Any, document_id: int | None):
    """Serialize Schicht A re-extraction of ONE document (Postgres advisory lock).

    Yields ``True`` when the caller holds the per-document lock, ``False`` when
    another re-extraction of the same document is already in flight (the caller
    must skip — the in-flight run refreshes the fact set, so skipping avoids the
    duplicate-fact race where two overlapping write-new-then-purge-old passes
    each leave their new set behind). No-op (always ``True``) on non-Postgres
    (the sqlite test harness has neither advisory locks nor concurrency).

    Non-blocking ``pg_try_advisory_lock`` (skip, don't wait) mirrors the KG
    reconciler: reindex is idempotent over the document's current content, so the
    winner's fresh set is always a valid refresh.

    The dedicated lock connection is acquired with a bounded timeout; if the pool
    can't hand one over in time (backlog), we DEGRADE to unlocked (yield ``True``)
    instead of blocking the ingest path — see ``_LOCK_CONN_ACQUIRE_TIMEOUT_S``.
    """
    dialect = bind.dialect.name if bind is not None else ""
    lock_engine = _resolve_lock_engine(bind) if dialect == "postgresql" else None
    if lock_engine is None or document_id is None:
        yield True
        return

    try:
        lock_conn = await asyncio.wait_for(
            lock_engine.connect(), timeout=_LOCK_CONN_ACQUIRE_TIMEOUT_S
        )
    except Exception as e:  # noqa: BLE001 — TimeoutError / pool / connect failure
        # Degrade to unlocked rather than block or fail the ingest: the guard is
        # best-effort, and a rare duplicate is reconciled by the next reindex.
        logger.warning(
            f"Schicht A: advisory-lock connection unavailable for doc "
            f"{document_id} ({e!r}); proceeding UNLOCKED"
        )
        yield True
        return

    try:
        got = bool((await lock_conn.execute(
            text("SELECT pg_try_advisory_lock(:ns, :doc)"),
            {"ns": _SCHICHT_A_REINDEX_LOCK_NS, "doc": int(document_id)},
        )).scalar())
        try:
            yield got
        finally:
            if got:
                # Explicit unlock; a checkin-hook (pg_advisory_unlock_all) is the
                # backstop if this raises or the connection dies.
                try:
                    await lock_conn.execute(
                        text("SELECT pg_advisory_unlock(:ns, :doc)"),
                        {"ns": _SCHICHT_A_REINDEX_LOCK_NS, "doc": int(document_id)},
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"Schicht A: advisory unlock failed for doc {document_id} "
                        f"({e!r}); checkin backstop will release it"
                    )
    finally:
        await lock_conn.close()


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

    Per-fact tier-override carry-over: before writing the fresh fact set, the
    prior facts that carry an owner-set per-fact tier override
    (``tier_overridden=True``) are snapshotted by content identity
    (``_fact_identity_key``). Each re-extracted fact that matches a snapshotted
    override re-acquires that tier + the sticky flag, so re-OCR/re-ingest no
    longer silently resets a deliberate per-fact override to the document tier.
    A fact whose content drifted enough not to match reverts to the doc tier
    (fail-safe: never more visible than the parent doc by default).
    """
    if not settings.schicht_a_extraction_enabled:
        return
    # §2 D14: never mine facts from a meeting transcript — small talk would
    # spawn phantom obligations/calendar events. Purpose-built action-item
    # extraction ships with the minutes phase, not here.
    from models.database import MEETING_TRANSCRIPT_SOURCE

    if kwargs.get("source") == MEETING_TRANSCRIPT_SOURCE:
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

            capped = result.facts[:_MAX_FACTS_PER_DOC]
            if len(result.facts) > _MAX_FACTS_PER_DOC:
                logger.warning(
                    f"Schicht A: doc {document_id} produced {len(result.facts)} "
                    f"facts; capping at {_MAX_FACTS_PER_DOC}"
                )

            # Serialize concurrent re-extraction of THIS document. Two overlapping
            # passes each do write-new-then-purge-old and would leave BOTH new sets
            # behind (duplicate facts). The loser skips — the winner's fresh set is a
            # complete refresh of the document's facts. Postgres-only advisory lock on
            # a DEDICATED connection (a session-level lock would drop at the mid-flight
            # commit below); a no-op on the sqlite test harness.
            async with _reindex_lock(db.bind, document_id) as got_lock:
                if not got_lock:
                    logger.info(
                        f"Schicht A: doc {document_id} re-extract already in flight; "
                        f"skipping (the in-flight pass refreshes the facts)"
                    )
                    return

                # Capture prior fact-atoms BEFORE writing — they're purged only AFTER
                # the new set is safely committed. AtomPurgeService.purge commits per
                # call, so a purge-then-write ordering would, on any write failure,
                # leave the document with ZERO facts. Write-new-then-purge-old means
                # the worst case is recoverable duplicates (cleaned on the next
                # reindex) — and the advisory lock above closes the concurrent window
                # that produced them — never silent loss of a document's facts.
                old_atom_ids = (await db.execute(
                    select(DocumentFact.atom_id).where(
                        DocumentFact.document_id == document_id
                    )
                )).scalars().all()

                # Carry-over snapshot: a per-fact tier OVERRIDE (e.g. a public issuer
                # on a private document) is bound to the current fact row, but a
                # re-extraction recreates the fact set from scratch — without this it
                # would silently reset to the doc tier. Snapshot the prior OVERRIDDEN
                # facts keyed by content identity so a re-extracted fact that matches
                # re-acquires its override. Only overrides need carrying; non-overridden
                # facts already follow the doc tier by default.
                prior_overrides = (await db.execute(
                    select(
                        DocumentFact.category,
                        DocumentFact.kind,
                        DocumentFact.normalized_value,
                        DocumentFact.value,
                        DocumentFact.circle_tier,
                    ).where(
                        DocumentFact.document_id == document_id,
                        DocumentFact.tier_overridden.is_(True),
                    )
                )).all()
                override_by_key: dict[tuple[str, str, str], int] = {
                    _fact_identity_key(r.category, r.kind, r.normalized_value, r.value):
                        int(r.circle_tier)
                    for r in prior_overrides
                }

                atom_svc = AtomService(db)
                stored = 0
                carried_over = 0
                for f in capped:
                    # If this fact matches a prior per-fact override, carry that tier
                    # + the sticky flag forward; otherwise it follows the doc tier.
                    # An override can never raise a fact ABOVE the document's own
                    # visibility intent in a way the owner didn't pick — they DID pick
                    # it (the prior PATCH), and a re-extraction must not silently undo
                    # that choice. (The override tier is whatever the owner set; it is
                    # NOT clamped to the doc tier — same as the live override.)
                    carried = override_by_key.get(
                        _fact_identity_key(
                            f.category, f.kind, f.normalized_value, f.value
                        )
                    )
                    fact_tier = carried if carried is not None else tier
                    is_override = carried is not None

                    atom_id = await atom_svc.create_with_source(
                        atom_type=ATOM_TYPE_DOCUMENT_FACT,
                        owner_user_id=int(owner_id),
                        tier=fact_tier,
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
                        circle_tier=fact_tier,
                        tier_overridden=is_override,
                    )
                    db.add(row)
                    await db.flush()
                    await atom_svc.finalize_source_id(atom_id, row.id)
                    stored += 1
                    if is_override:
                        carried_over += 1

                await db.commit()

                # New facts are committed; now retire the prior set.
                for aid in old_atom_ids:
                    if aid:
                        await AtomPurgeService.purge(
                            db, atom_id=aid, reason="schicht_a_reextract"
                        )

                logger.info(
                    f"Schicht A: stored {stored} fact(s) for doc {document_id} "
                    f"(tier={tier}, carried-over overrides={carried_over})"
                )

            # Synthesize a human-meaningful display title from the facts (best-
            # effort; a miss leaves the metadata title / filename as-is). Outside the
            # re-extract lock — it touches only documents.generated_title, not facts.
            try:
                title = await generate_document_title(capped, lang=lang)
                if title:
                    doc.generated_title = title
                    await db.commit()
            except Exception as e:  # noqa: BLE001 — title is non-essential
                logger.warning(f"Schicht A: title synthesis failed for doc {document_id}: {e}")
    except Exception as e:  # noqa: BLE001 — never fail the ingest on a fact miss
        logger.warning(f"Schicht A post_document_ingest hook failed: {e}")
