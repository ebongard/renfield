"""Contact points from documents: the issuer's own numbers (#1240 stage 4).

Design: ``docs/design/kg-contact-points.md`` §8.3 and §14. Deterministic, no LLM.

A number is taken only when ALL of these hold:
  1. a keyword stands right before it on the same line (``Tel``, ``Telefon``,
     ``Fax``, ``Mobil`` ...); a bare digit run never counts;
  2. it validates with ``phonenumbers`` (``normalize_phone``);
  3. it stands NEAR the issuer's name (``ISSUER_WINDOW`` characters). The stored
     text has no reliable page geometry (a third of the chunks carry no page
     number), so "letterhead or footer" is read as "in the issuer's own block":
     a collection agency's number sits by the agency's name, not the client's;
  4. it is not in the recipient block (a ``party`` fact naming the recipient);
  5. exactly ONE live person/organization of the document's owner carries the
     issuer's name or alias (a counting query, never ``resolve_entity``: that
     takes ``.first()`` across tiers and bumps ``mention_count``). Zero or more
     than one = skipped, never guessed, never created.

A document value is never "confirmed": ``source_type='document'``, ranked after a
manual value, and hidden on READ while the same number is live on another entity
of the same owner (``contact_points.DOCUMENT_AMBIGUITY_SQL``) -- a tax advisor's or
property manager's number printed on letters for many issuers names nobody.

Tier: ``source_tier`` = the document's tier; the ``kb_document`` cascade
(``cap_document_contact_points``) lowers it with the document. ``document_id`` is
a real FK, so deleting the document takes its values along. A value keeps the tier
of the document it was FIRST read from: a later, more open document does not lift
it (an owner's lowered tier must survive a re-ingest, and a lift would need to tell
the two apart).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    CONTACT_POINT_KIND_FAX,
    CONTACT_POINT_KIND_MOBILE,
    CONTACT_POINT_KIND_PHONE,
)
from utils.config import settings

CONTACT_POINT_SOURCE_DOCUMENT = "document"

# How far (characters, either side) a number may stand from the issuer's name.
ISSUER_WINDOW = 300
# A document value is a suggestion: below a manual entry, above nothing.
DOCUMENT_CONFIDENCE = 0.6
# Fact kinds that name the issuer / the recipient (free LLM labels, folded).
ISSUER_KINDS = ("aussteller", "issuer", "absender")
RECIPIENT_KIND_PREFIXES = ("empf", "recipient", "adressat")

_KIND_BY_KEYWORD = {
    "fax": CONTACT_POINT_KIND_FAX, "telefax": CONTACT_POINT_KIND_FAX, "f": CONTACT_POINT_KIND_FAX,
    "mobil": CONTACT_POINT_KIND_MOBILE, "mobile": CONTACT_POINT_KIND_MOBILE,
    "handy": CONTACT_POINT_KIND_MOBILE, "m": CONTACT_POINT_KIND_MOBILE,
    "faxnummer": CONTACT_POINT_KIND_FAX, "telefaxnummer": CONTACT_POINT_KIND_FAX,
    "mobilnummer": CONTACT_POINT_KIND_MOBILE, "handynummer": CONTACT_POINT_KIND_MOBILE,
}

# keyword, optional "." / ":" , then the number ON THE SAME LINE (no \n inside:
# a phone at the end of a line must not swallow the postcode on the next one).
# The one-letter forms (T / F / M) count only with a ":" or "." after them or a
# "+" / "0" right behind -- "T 0221 ..." in a footer, never a stray capital.
_NUMBER = r"((?:\+|00|\()?\d[\d \t/().\-–]{4,24}\d)"
_KEYWORD_RE = re.compile(
    r"(?i)(?<![A-Za-z\u00c0-\u017f])"
    r"(telefonnummer|telefaxnummer|faxnummer|mobilnummer|handynummer|rufnummer"
    r"|tel\.?[ \t]?-?[ \t]?nr|telefax|telefon|tel|fon|phone|fax|mobil(?:e|funk)?|handy|hotline"
    r"|[tfm](?=[ \t]{0,3}[:.]|[ \t]{0,3}(?:\+|0)))"
    r"(?![A-Za-z\u00c0-\u017f])"
    # an optional WORD label in parentheses ("Tel. (Zentrale):"), never a
    # parenthesized area code -- that belongs to the number. Every gap is
    # BOUNDED ({0,5}): unbounded neighbouring runs backtrack polynomially on a
    # keyword followed by a long run of blanks (padded table columns).
    r"\.?[ \t]{0,5}(?:\([A-Za-z\u00c0-\u017f][^)\n]{0,20}\)[ \t]{0,5})?[:.]?[ \t]{0,5}" + _NUMBER
)
_WS_RE = re.compile(r"\s+")
_LEGAL_FORM_RE = re.compile(
    r"(?i)[\s,]+(gmbh(?:\s*&\s*co\.?\s*kg)?|mbh|ag|kg|ohg|e\.\s?v\.|ev|se|ug(?:\s*\(haftungsbeschränkt\))?)\.?$"
)


@dataclass(frozen=True)
class DocumentNumber:
    kind: str
    value: str        # verbatim, as printed
    normalized: str   # E.164


@dataclass
class SyncResult:
    candidates: int = 0
    stored: int = 0
    unchanged: int = 0
    removed: int = 0
    rejected: int = 0
    no_entity: int = 0
    ambiguous_entity: int = 0
    in_recipient_block: int = 0
    skipped_reason: str | None = None
    details: list[str] = field(default_factory=list)


def _name_pattern(name: str) -> re.Pattern | None:
    tokens = [re.escape(t) for t in _WS_RE.split(name.strip()) if t]
    if not tokens:
        return None
    # Whole words: "Bau" (from "Bau GmbH") must not match inside "Baumarkt".
    return re.compile(r"(?i)(?<!\w)" + r"\s+".join(tokens) + r"(?!\w)")


def _issuer_spans(text_: str, issuer_names: list[str]) -> list[tuple[int, int]]:
    """Where the issuer's name stands, with and without its legal form."""
    spans: list[tuple[int, int]] = []
    for name in issuer_names:
        variants = {name.strip(), _LEGAL_FORM_RE.sub("", name.strip())}
        for v in variants:
            if len(v) < 3:
                continue
            pat = _name_pattern(v)
            if pat is not None:
                spans.extend(m.span() for m in pat.finditer(text_))
    return spans


def find_issuer_numbers(
    text_: str, issuer_names: list[str], recipient_texts: list[str] | None = None,
) -> tuple[list[DocumentNumber], int]:
    """The issuer's numbers in ``text_`` and how many were dropped as recipient numbers.

    Pure; rules 1-4 of the module docstring. De-duplicated by (kind, E.164).
    """
    from services.contact_points import normalize_phone

    if not text_ or not issuer_names:
        return [], 0
    spans = _issuer_spans(text_, issuer_names)
    if not spans:
        return [], 0
    recipient_numbers: set[str] = set()
    for rt in recipient_texts or []:
        for m in _KEYWORD_RE.finditer(rt or ""):
            n = normalize_phone(m.group(2).strip())
            if n:
                recipient_numbers.add(n[0])
        for m in re.finditer(_NUMBER, rt or ""):
            n = normalize_phone(m.group(1).strip())
            if n:
                recipient_numbers.add(n[0])

    found: dict[tuple[str, str], DocumentNumber] = {}
    dropped = 0
    for m in _KEYWORD_RE.finditer(text_):
        start = m.start()
        if not any(s - ISSUER_WINDOW <= start <= e + ISSUER_WINDOW for s, e in spans):
            continue
        # A column gap (two or more blanks) ends the number: a layout row
        # "Tel 030 1234567   01.02.2024" must not swallow the date beside it.
        raw = re.split(r"[ \t]{2,}", m.group(2))[0].strip(" \t-./(")
        normalized = normalize_phone(raw)
        if normalized is None:
            continue
        e164, detected = normalized
        if e164 in recipient_numbers:
            dropped += 1
            continue
        kind = _KIND_BY_KEYWORD.get(m.group(1).lower(), CONTACT_POINT_KIND_PHONE)
        if kind == CONTACT_POINT_KIND_PHONE and detected == CONTACT_POINT_KIND_MOBILE:
            kind = CONTACT_POINT_KIND_MOBILE
        found.setdefault((kind, e164), DocumentNumber(kind, raw, e164))
    return list(found.values()), dropped


async def _document_context(db: AsyncSession, document_id: int):
    row = (await db.execute(text(
        "SELECT d.circle_tier, a.owner_user_id FROM documents d "
        "LEFT JOIN atoms a ON a.atom_id = d.atom_id WHERE d.id = :d"
    ), {"d": int(document_id)})).first()
    if row is None:
        return None
    facts = (await db.execute(text(
        "SELECT lower(kind) AS kind, value, excerpt FROM document_facts WHERE document_id = :d"
    ), {"d": int(document_id)})).all()
    issuers = [f.value for f in facts if f.kind in ISSUER_KINDS and f.value]
    recipients = [
        " ".join(x for x in (f.value, f.excerpt) if x)
        for f in facts if f.kind.startswith(RECIPIENT_KIND_PREFIXES)
    ]
    return int(row.circle_tier or 0), row.owner_user_id, issuers, recipients


async def match_issuer_entity(db: AsyncSession, name: str, owner_id: int) -> tuple[int | None, int]:
    """``(entity_id, match count)``: the id only when EXACTLY one entity matches."""
    rows = (await db.execute(text(
        "SELECT id FROM kg_entities e "
        "WHERE e.is_active AND e.canonical_id IS NULL AND e.user_id = :o "
        "AND e.entity_type IN ('organization', 'person') "
        "AND (lower(e.name) = lower(:n) OR EXISTS ("
        "  SELECT 1 FROM jsonb_array_elements_text(COALESCE(e.surface_forms, '[]')::jsonb) sf "
        "  WHERE lower(sf) = lower(:n))) "
        "LIMIT 2"
    ), {"o": int(owner_id), "n": name.strip()})).all()
    return (int(rows[0].id) if len(rows) == 1 else None), len(rows)


async def sync_document_contact_points(
    db: AsyncSession, document_id: int, text_: str, *, commit: bool = True,
) -> SyncResult:
    """Bring this document's values in line with what its text yields now (no LLM).

    Idempotent and edit-preserving: a value still derived is left UNTOUCHED (an
    owner's lowered tier or label survives a re-ingest); a value no longer derived
    from this document is removed; a new one is inserted. Never re-inserted: a
    value the owner removed (``ContactPointService.delete`` ends a document value
    instead of deleting it -- the ended row is the tombstone). A value the entity
    already holds live from any source is left alone. The INSERT is
    ``ON CONFLICT DO NOTHING`` on the live unique index, so a concurrent ingest
    and backfill cannot fail each other.
    """
    from services.contact_point_tier import contact_point_tier

    res = SyncResult()
    ctx = await _document_context(db, document_id)
    if ctx is None:
        res.skipped_reason = "no_document"
        return res
    tier, owner_id, issuers, recipients = ctx
    if owner_id is None:
        res.skipped_reason = "no_owner"
        return res
    if not issuers:
        res.skipped_reason = "no_issuer"
        return res

    numbers, res.in_recipient_block = find_issuer_numbers(text_, issuers, recipients)
    res.candidates = len(numbers)

    entity_id: int | None = None
    if numbers:
        most = 0
        for name in issuers:
            entity_id, count = await match_issuer_entity(db, name, int(owner_id))
            most = max(most, count)
            if entity_id is not None:
                break
        if entity_id is None:
            if most > 1:
                res.ambiguous_entity = len(numbers)
            else:
                res.no_entity = len(numbers)
            numbers = []

    derived = {(entity_id, n.normalized) for n in numbers}
    prior = (await db.execute(text(
        "SELECT id, entity_id, normalized FROM kg_entity_contact_points "
        "WHERE document_id = :d AND source_type = :s AND valid_to IS NULL"
    ), {"d": int(document_id), "s": CONTACT_POINT_SOURCE_DOCUMENT})).all()
    stale = [r.id for r in prior if (r.entity_id, r.normalized) not in derived]
    if stale:
        await db.execute(text("DELETE FROM kg_entity_contact_points WHERE id = ANY(:ids)"), {"ids": stale})
        res.removed = len(stale)

    if entity_id is not None:
        etier = int((await db.execute(text(
            "SELECT circle_tier FROM kg_entities WHERE id = :e"), {"e": entity_id})).scalar() or 0)
        for n in numbers:
            seen = (await db.execute(text(
                "SELECT bool_or(valid_to IS NULL) AS live, "
                "bool_or(valid_to IS NOT NULL AND superseded_by_id IS NULL AND source_type = :s) AS removed "
                "FROM kg_entity_contact_points WHERE entity_id = :e AND normalized = :n"
            ), {"e": entity_id, "n": n.normalized, "s": CONTACT_POINT_SOURCE_DOCUMENT})).one()
            if seen.live:
                res.unchanged += 1
                continue
            if seen.removed:
                res.rejected += 1
                continue
            inserted = (await db.execute(text(
                "INSERT INTO kg_entity_contact_points (entity_id, user_id, kind, value, normalized, "
                "source_type, document_id, confidence, source_tier, circle_tier, valid_from, created_at) "
                "VALUES (:e, :o, :k, :v, :n, :s, :d, :c, :t, :ct, now(), now()) "
                "ON CONFLICT (entity_id, kind, normalized) WHERE valid_to IS NULL DO NOTHING "
                "RETURNING id"
            ), {
                "e": entity_id, "o": int(owner_id), "k": n.kind, "v": n.value[:64], "n": n.normalized,
                "s": CONTACT_POINT_SOURCE_DOCUMENT, "d": int(document_id), "c": DOCUMENT_CONFIDENCE,
                "t": tier, "ct": contact_point_tier(etier, tier),
            })).first()
            if inserted is None:
                res.unchanged += 1
            else:
                res.stored += 1
    if commit:
        await db.commit()
    return res


SCAN_COUNT_KEYS = (
    "candidates", "stored", "unchanged", "removed", "rejected",
    "no_entity", "ambiguous_entity", "in_recipient_block",
)


async def document_text(db: AsyncSession, document_id: int) -> str:
    """The stored text: top-level chunks only (a child repeats its parent's text)."""
    rows = (await db.execute(text(
        "SELECT content FROM document_chunks WHERE document_id = :d AND parent_chunk_id IS NULL "
        "ORDER BY chunk_index NULLS LAST, id"
    ), {"d": int(document_id)})).scalars().all()
    return "\n".join(r for r in rows if r)


async def scan_documents(
    db: AsyncSession, *, owner_id: int | None, commit: bool, limit: int | None = None,
    time_budget_s: float | None = None,
) -> dict[str, int]:
    """Re-derive the values of every document with an issuer fact; COUNTS only.

    ``owner_id`` scopes to that owner's documents (the document atom's owner);
    None = every document (the operator backfill, or auth off). A failing document
    is rolled back and counted, never ends the run. ``commit=False`` rolls every
    document back (a dry run). ``time_budget_s`` stops between documents once
    spent and reports the rest as ``remaining`` (a chat turn must not block for
    minutes); the run is idempotent, so a second one finishes the job.
    """
    import time
    from collections import Counter

    sql = (
        "SELECT DISTINCT f.document_id FROM document_facts f "
        "JOIN documents d ON d.id = f.document_id "
        "LEFT JOIN atoms a ON a.atom_id = d.atom_id "
        "WHERE lower(f.kind) = ANY(:k)"
    )
    params: dict = {"k": list(ISSUER_KINDS)}
    if owner_id is not None:
        sql += " AND a.owner_user_id = :o"
        params["o"] = int(owner_id)
    sql += " ORDER BY f.document_id"
    if limit:
        sql += " LIMIT :lim"
        params["lim"] = int(limit)
    ids = (await db.execute(text(sql), params)).scalars().all()

    totals: Counter = Counter()
    deadline = time.monotonic() + time_budget_s if time_budget_s else None
    for i, doc_id in enumerate(ids):
        if deadline is not None and time.monotonic() > deadline:
            totals["remaining"] = len(ids) - i
            break
        try:
            body = await document_text(db, doc_id)
            res = await sync_document_contact_points(db, doc_id, body, commit=commit)
        except Exception as e:  # one bad document must not end the run
            await db.rollback()
            totals["failed"] += 1
            logger.warning(f"Contact points scan: doc {doc_id} failed: {type(e).__name__}")
            continue
        if not commit:
            await db.rollback()
        totals["documents"] += 1
        if res.skipped_reason:
            totals[f"skipped_{res.skipped_reason}"] += 1
        if res.candidates:
            totals["documents_with_numbers"] += 1
        for key in SCAN_COUNT_KEYS:
            totals[key] += getattr(res, key)
    return dict(totals)


async def contact_points_post_extraction(document_id: int, field_text: str) -> None:
    """Ingest seam, called by the Schicht-A hook once the issuer fact is committed.

    Best-effort: a failure logs and returns; ingest never fails on it.
    """
    if not (settings.contact_points_enabled and settings.contact_points_from_documents_enabled):
        return
    from services.database import AsyncSessionLocal
    from utils.log_safety import error_text_without_content

    try:
        async with AsyncSessionLocal() as db:
            res = await sync_document_contact_points(db, document_id, field_text)
        if res.stored:
            logger.info(f"Contact points: doc {document_id} stored {res.stored} issuer number(s)")
    except Exception as e:
        logger.warning(
            f"Contact points from doc {document_id} failed: {error_text_without_content(e)}"
        )
