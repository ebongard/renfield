"""
Watch-folder PDF → Simba review flow (xidra only).

A watch-folder-ingested PDF that should reach the Simba tax portal is NEVER
auto-uploaded (the transfer to the tax accountant is irreversible). Instead the
document-worker's post-ingest hook classifies it and files a PENDING
``simba_ingest_proposals`` row with a category/type SUGGESTION; the owner
confirms it on /brain/review — optionally editing category/type — which triggers
the real upload, or rejects it.

- Classification uses a stable built-in taxonomy (KNOWN_SIMBA_TAXONOMY) so the
  worker needs no simba MCP client. The review UI fetches the LIVE taxonomy for
  the dropdown via the backend (which has the mcp_manager).
- Gated on ``folder_ingest_simba_enabled`` (re-asserted in the hook, H4) +
  ``source == 'folder_ingest'`` + a .pdf filename.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from models.database import (
    FOLDER_INGEST_SOURCE,
    SIMBA_PROPOSAL_PENDING,
    SIMBA_PROPOSAL_REJECTED,
    SIMBA_PROPOSAL_UPLOADED,
    SIMBA_PROPOSAL_UPLOADING,
    Atom,
    Document,
    DocumentChunk,
    DocumentFact,
    SimbaIngestProposal,
)
from services.database import AsyncSessionLocal
from utils.config import settings

# Stable Simba taxonomy (mirrors the MCP's KNOWN_CATEGORIES) — used for the
# ingest-time classification so the worker needs no simba MCP client.
KNOWN_SIMBA_TAXONOMY: dict[str, list[str]] = {
    "Belege": ["Ausgangsrechnung", "Eingangsrechnung", "Kassenbeleg", "Barbeleg", "Kreditkartenbeleg", "EC-Beleg"],
    "Posteingang": ["Bescheide", "Mahnungen", "Schriftverkehr", "Schriftverkehr mit Frist", "Steuerunterlagen", "Kontoauszug Bezahlsystem"],
    "Lohn": ["Lohnunterlagen"],
    "Transferdateien": ["Buchungsdaten"],
    "Privat": ["Diverses"],
}


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# The Simba portal's "Bezeichnung" (description) accepts only this charset — it
# MUST mirror the simba MCP's DEFAULT_TEXT_PATTERN (letters+digits+German
# umlauts+space . _ -). Any other char (em-dash, comma, colon, slash, …) makes
# the MCP reject the upload, so we sanitize renfield-side rather than send a raw
# title that would fail.
_SIMBA_DESC_DISALLOWED = re.compile(r"[^A-Za-z0-9ÄÖÜäöüß ._-]+")
_SIMBA_DESC_MAX = 100


def _sanitize_desc(raw: str | None) -> str:
    """Coerce any string to the Simba portal's allowed Bezeichnung charset
    (disallowed chars → space, whitespace collapsed, capped). Applied to both the
    auto-derived title AND a user-edited value, so neither can break the upload."""
    cleaned = _SIMBA_DESC_DISALLOWED.sub(" ", raw or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:_SIMBA_DESC_MAX].strip()


def _bezeichnung(doc) -> str:
    """Human 'Bezeichnung' for a Simba upload from the folder-ingest review flow.

    Source = the document's generated title (Schicht A) → title → filename stem;
    sanitized to the portal's allowed charset and capped at 100 chars. Used to
    prefill the (editable) review field and as the fallback when the user leaves
    it blank — unlike the chat-menu / bridge paths, the review flow had no
    description at all before this.
    """
    raw = (
        getattr(doc, "generated_title", None)
        or getattr(doc, "title", None)
        or Path(doc.filename or "").stem
        or ""
    ).strip()
    return _sanitize_desc(raw)


# Date parsing for the Simba booking period (Zeitraum). Handles ISO (2026-03-18)
# and German DD.MM.YYYY (18.03.2026) — the two forms Schicht-A facts / titles use.
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DATE_DMY = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b")


def _parse_month_year(s: str | None) -> tuple[int, int] | None:
    """(month, year) from a date string (ISO first, then DD.MM.YYYY), or None."""
    if not s:
        return None
    m = _DATE_ISO.search(s)
    if m:
        yy, mm = int(m.group(1)), int(m.group(2))
    else:
        m = _DATE_DMY.search(s)
        if not m:
            return None
        mm, yy = int(m.group(2)), int(m.group(3))
    if 1 <= mm <= 12 and 2000 <= yy <= 2100:
        return mm, yy
    return None


async def _document_period(db, document_id: int, doc=None) -> tuple[int | None, int | None]:
    """Booking period (month, year) for the Simba upload, derived from the
    document's OWN date so the review form does not default to the CURRENT month
    (the #1167 bug — every upload was stamped with today's month). Prefers the
    Schicht-A ``rechnungsdatum`` fact, then other date-bearing facts, then a date
    parsed from the generated title (``… vom 18.03.2026``). (None, None) if no
    date is derivable — the UI then falls back to now."""
    def _rank(kind: str | None) -> int:
        k = (kind or "").lower()
        if "rechnungsdatum" in k:
            return 0
        if "leistung" in k or "datum" in k or "date" in k:
            return 1
        return 2

    try:
        rows = (
            await db.execute(
                select(DocumentFact.kind, DocumentFact.normalized_value, DocumentFact.value)
                .where(DocumentFact.document_id == document_id)
            )
        ).all()
        for kind, nv, v in sorted(rows, key=lambda r: _rank(r[0])):
            got = _parse_month_year(nv) or _parse_month_year(v)
            if got:
                return got
    except Exception as e:  # noqa: BLE001 — period is best-effort
        logger.debug(f"simba period: fact-date lookup failed for doc {document_id}: {e}")

    try:
        if doc is None:
            doc = await db.get(Document, document_id)
        title = (getattr(doc, "generated_title", None) or getattr(doc, "title", None) or "") if doc else ""
        got = _parse_month_year(title)
        if got:
            return got
    except Exception:  # noqa: BLE001
        pass
    return None, None


async def simba_ingest_post_hook(
    chunks: list[str] | None = None,
    document_id: int | None = None,
    user_id: int | None = None,
    field_text: str = "",
    lang: str | None = None,
    **kwargs: Any,
) -> None:
    """post_document_ingest consumer: file a PENDING Simba review proposal for a
    watch-folder PDF. Best-effort — never affects the KB/Paperless legs."""
    if not settings.folder_ingest_simba_enabled or document_id is None:
        return
    async with AsyncSessionLocal() as db:
        doc = (
            await db.execute(select(Document).where(Document.id == document_id))
        ).scalar_one_or_none()
        if doc is None or doc.source != FOLDER_INGEST_SOURCE:
            return
        if not (doc.filename or "").lower().endswith(".pdf"):
            return
        # Idempotency: one proposal per document (any status).
        exists = (
            await db.execute(
                select(SimbaIngestProposal.id)
                .where(SimbaIngestProposal.document_id == document_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if exists is not None:
            return

        # Owner = the document's atom owner (authoritative), else the ingesting
        # user. A null owner is fine here (the proposal is admin-visible).
        owner_id = await _resolve_doc_owner(db, doc, fallback=user_id)

        category = type_ = None
        try:
            from services.simba_classify import classify_simba

            category, type_ = await classify_simba(
                field_text or "", KNOWN_SIMBA_TAXONOMY, lang=lang or "de"
            )
        except Exception as e:  # noqa: BLE001 — classification is optional
            logger.warning(f"simba-ingest: classify failed for doc {document_id}: {e}")

        try:
            db.add(
                SimbaIngestProposal(
                    document_id=document_id,
                    user_id=owner_id,
                    filename=doc.filename,
                    suggested_category=category,
                    suggested_type=type_,
                    status=SIMBA_PROPOSAL_PENDING,
                )
            )
            await db.commit()
            logger.info(
                f"simba-ingest: review proposal for doc {document_id} "
                f"(suggest {category}/{type_})"
            )
        except IntegrityError as e:
            # Benign: a concurrent hook already filed the pending proposal
            # (partial-unique uq_simba_ingest_proposals_pending_doc).
            await db.rollback()
            logger.info(f"simba-ingest: proposal already pending for doc {document_id}: {e}")
        except Exception as e:  # noqa: BLE001
            # Real failure (FK/connection/serialization) — this hook is the ONLY
            # path that surfaces the PDF for Simba review, so a silent drop means
            # the document never reaches the queue. Log loud, not as a race.
            await db.rollback()
            logger.warning(
                f"simba-ingest: proposal insert FAILED for doc {document_id} "
                f"(will NOT surface for Simba review): {e}"
            )


# ---------------------------------------------------------------------------
# Review actions (backed by api/routes/simba_ingest.py)
# ---------------------------------------------------------------------------


def _is_admin(user) -> bool:
    if user is None:
        return False
    try:
        from models.permissions import Permission, has_permission

        return has_permission(user.get_permissions(), Permission.ADMIN)
    except Exception:  # noqa: BLE001 — permission parse must not 500 the route
        return False


async def _resolve_doc_owner(db, doc, fallback: int | None) -> int | None:
    """A document's owner = its atom owner (authoritative; Document has no
    user_id column), else the given fallback. Shared by the ingest hook and the
    send-existing-document path."""
    owner_id = fallback
    if getattr(doc, "atom_id", None):
        doc_atom = (
            await db.execute(select(Atom).where(Atom.atom_id == doc.atom_id))
        ).scalar_one_or_none()
        if doc_atom is not None:
            owner_id = doc_atom.owner_user_id
    return owner_id


def _can_send_document(owner_id: int | None, user) -> bool:
    """Access gate for sending an EXISTING KB document to Simba: auth-off sees
    all; else the document's owner, or an admin (incl. an ownerless document)."""
    if not settings.auth_enabled:
        return True
    if user is None or getattr(user, "id", None) is None:
        return False
    if owner_id is not None and owner_id == user.id:
        return True
    return _is_admin(user)  # an admin may send any (incl. ownerless) document


def _owns(proposal: SimbaIngestProposal, user) -> bool:
    """Ownership gate (mirrors pdf_split ``_owned_proposal``).

    - Auth OFF (single-user) → sees everything.
    - Auth ON, unauthenticated (user None) → denied (the routes also 401 first).
    - Auth ON → the proposal's owner, or an ADMIN for an ownerless proposal
      (so a null-owner folder-ingest proposal can't be seen/acted on by every
      logged-in user — only its owner or an admin).
    """
    if not settings.auth_enabled:
        return True
    if user is None or getattr(user, "id", None) is None:
        return False
    if proposal.user_id == user.id:
        return True
    return proposal.user_id is None and _is_admin(user)


async def list_pending(
    db, user
) -> list[tuple[SimbaIngestProposal, str, int | None, int | None]]:
    """Owner-scoped pending proposals, each paired with a suggested Bezeichnung
    (from the document's title) and a suggested booking period (month, year) from
    the document's date — both prefill the editable review fields, so the period
    defaults to the invoice month rather than the current one (#1167)."""
    q = (
        select(SimbaIngestProposal, Document)
        .join(Document, Document.id == SimbaIngestProposal.document_id, isouter=True)
        .where(SimbaIngestProposal.status == SIMBA_PROPOSAL_PENDING)
        .order_by(SimbaIngestProposal.id.desc())
    )
    rows = (await db.execute(q)).all()
    out: list[tuple[SimbaIngestProposal, str, int | None, int | None]] = []
    for (p, doc) in rows:
        if not _owns(p, user):
            continue
        desc = _bezeichnung(doc) if doc is not None else ""
        month, year = await _document_period(db, p.document_id, doc) if doc is not None else (None, None)
        out.append((p, desc, month, year))
    return out


async def _classify_document(db, document_id: int) -> tuple[str | None, str | None]:
    """Classify an EXISTING document's stored chunk text into a Simba
    category/type suggestion (best-effort — returns (None, None) on any failure)."""
    try:
        rows = (
            await db.execute(
                select(DocumentChunk.content)
                .where(DocumentChunk.document_id == document_id)
                .order_by(DocumentChunk.id.asc())
                .limit(20)
            )
        ).scalars().all()
        text = " ".join(c for c in rows if c)[:8000]
        if not text.strip():
            return None, None
        from services.simba_classify import classify_simba

        return await classify_simba(text, KNOWN_SIMBA_TAXONOMY, lang="de")
    except Exception as e:  # noqa: BLE001 — classification is optional
        logger.warning(f"simba-ingest: classify failed for existing doc {document_id}: {e}")
        return None, None


async def create_proposal_for_document(db, document_id: int, user) -> dict:
    """Create a PENDING Simba review proposal for an EXISTING knowledge-base
    document (the "send to Simba" action — complements the folder-ingest hook,
    which only fires on NEW documents; a doc already in the KB is deduped at
    ingest and never reaches that hook).

    Returns {"success": bool, "message": str, "proposal_id": int|None,
    "suggested_category": str|None, "suggested_type": str|None,
    "suggested_description": str}. The suggested_* fields let the caller (the
    doc-page "send to Simba" overlay) prefill the confirm form without a second
    fetch. Idempotent on the pending state: a doc that already has a pending
    proposal returns it rather than creating a second (the partial-unique index
    also guards this). Owner/admin-gated.
    """
    doc = await db.get(Document, document_id)
    if doc is None:
        return {"success": False, "message": "not_found", "proposal_id": None}

    # fallback=None (NOT the caller): an atom-less / unresolved-owner document is
    # admin-only, never auto-owned by whoever asked — else any authenticated user
    # could queue someone else's document for the irreversible upload. Mirrors the
    # null-owner→admin rule in _owns.
    owner_id = await _resolve_doc_owner(db, doc, fallback=None)
    if not _can_send_document(owner_id, user):
        # 404-style: don't leak existence to a non-owner.
        return {"success": False, "message": "not_found", "proposal_id": None}

    if not (doc.filename or "").strip():
        return {"success": False, "message": "document has no filename", "proposal_id": None}
    if not doc.file_path:
        return {"success": False, "message": "document file no longer available", "proposal_id": None}

    # Idempotency: reuse an existing pending proposal for this document.
    existing = (
        await db.execute(
            select(SimbaIngestProposal)
            .where(
                SimbaIngestProposal.document_id == document_id,
                SimbaIngestProposal.status == SIMBA_PROPOSAL_PENDING,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    # Bezeichnung prefill — a pure function of the document (generated_title →
    # title → filename), the same value list_pending pairs with each row.
    desc = _bezeichnung(doc)
    # Booking period (Zeitraum) from the document's OWN date — so the review form
    # defaults to the invoice month, not the current month (#1167).
    pmonth, pyear = await _document_period(db, document_id, doc)
    if existing is not None:
        return {
            "success": True, "message": "already_pending", "proposal_id": existing.id,
            "suggested_category": existing.suggested_category,
            "suggested_type": existing.suggested_type,
            "suggested_description": desc,
            "suggested_month": pmonth, "suggested_year": pyear,
        }

    category, type_ = await _classify_document(db, document_id)
    proposal = SimbaIngestProposal(
        document_id=document_id,
        user_id=owner_id,
        filename=doc.filename,
        suggested_category=category,
        suggested_type=type_,
        status=SIMBA_PROPOSAL_PENDING,
    )
    try:
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
    except IntegrityError:
        # A concurrent send won the pending slot — return that one.
        await db.rollback()
        again = (
            await db.execute(
                select(SimbaIngestProposal).where(
                    SimbaIngestProposal.document_id == document_id,
                    SimbaIngestProposal.status == SIMBA_PROPOSAL_PENDING,
                ).limit(1)
            )
        ).scalar_one_or_none()
        return {
            "success": True, "message": "already_pending",
            "proposal_id": again.id if again else None,
            "suggested_category": again.suggested_category if again else category,
            "suggested_type": again.suggested_type if again else type_,
            "suggested_description": desc,
            "suggested_month": pmonth, "suggested_year": pyear,
        }
    logger.info(f"simba-ingest: review proposal for existing doc {document_id} (send-to-simba)")
    return {
        "success": True, "message": "created", "proposal_id": proposal.id,
        "suggested_category": category, "suggested_type": type_,
        "suggested_description": desc,
        "suggested_month": pmonth, "suggested_year": pyear,
    }


async def reject(db, proposal_id: int, user) -> bool:
    """Mark a pending proposal rejected. Returns False if not found / not owned /
    already resolved."""
    p = await db.get(SimbaIngestProposal, proposal_id)
    if p is None or not _owns(p, user):
        return False
    # Conditional update so two concurrent resolves can't both win.
    res = await db.execute(
        update(SimbaIngestProposal)
        .where(
            SimbaIngestProposal.id == proposal_id,
            SimbaIngestProposal.status == SIMBA_PROPOSAL_PENDING,
        )
        .values(
            status=SIMBA_PROPOSAL_REJECTED,
            resolved_at=_utcnow(),
            resolved_by_user_id=getattr(user, "id", None),
        )
    )
    await db.commit()
    return res.rowcount > 0


async def _claim_pending(db, proposal_id: int) -> bool:
    """Atomically flip PENDING → UPLOADING so exactly one confirm() proceeds to
    the irreversible upload. Returns True iff this caller won the claim."""
    res = await db.execute(
        update(SimbaIngestProposal)
        .where(
            SimbaIngestProposal.id == proposal_id,
            SimbaIngestProposal.status == SIMBA_PROPOSAL_PENDING,
        )
        .values(status=SIMBA_PROPOSAL_UPLOADING)
    )
    await db.commit()
    return res.rowcount > 0


async def _revert_claim(db, proposal_id: int) -> None:
    """Undo a claim (UPLOADING → PENDING) after a failed upload so the owner can
    retry. Conditional on UPLOADING so a concurrent resolve can't be clobbered."""
    await db.execute(
        update(SimbaIngestProposal)
        .where(
            SimbaIngestProposal.id == proposal_id,
            SimbaIngestProposal.status == SIMBA_PROPOSAL_UPLOADING,
        )
        .values(status=SIMBA_PROPOSAL_PENDING)
    )
    await db.commit()


async def confirm(
    db, proposal_id: int, category: str, type_: str, user, mcp_manager,
    description: str | None = None, month: int | None = None, year: int | None = None,
    force: bool = False,
) -> dict:
    """Confirm a pending proposal → REAL upload to Simba, then mark uploaded.

    ``description`` is the (editable) Bezeichnung from the review UI — sanitized
    and used when non-empty, else auto-derived from the document title.

    ``month``/``year`` are the Simba booking period (Buchungszeitraum). The MCP
    silently defaults an omitted period to the CURRENT month/year, so the review
    UI always sends an explicit (user-editable) value — otherwise every upload
    is mis-stamped with "now" instead of the document's actual period.

    Returns {"success": bool, "message": str}. The proposal is only marked
    uploaded when the document actually landed (uebertragen>0).

    The upload to the tax accountant is IRREVERSIBLE and the portal forbids
    withdrawal, so this MUST NOT double-upload. Pre-conditions are validated
    first, then the row is CLAIMED (PENDING → UPLOADING via a conditional UPDATE)
    BEFORE the upload — two concurrent confirms can't both pass the claim, so
    only the winner uploads. A claim is reverted on any non-landed outcome.
    """
    p = await db.get(SimbaIngestProposal, proposal_id)
    if p is None or not _owns(p, user):
        return {"success": False, "message": "not_found"}
    if p.status != SIMBA_PROPOSAL_PENDING:
        return {"success": False, "message": "already_resolved"}
    if not category.strip() or not type_.strip():
        return {"success": False, "message": "category and type required"}
    if mcp_manager is None:
        return {"success": False, "message": "MCP not available"}

    doc = await db.get(Document, p.document_id)
    if doc is None or not doc.file_path or not Path(doc.file_path).is_file():
        return {"success": False, "message": "document file no longer available"}

    # User-edited Bezeichnung wins when it survives sanitization; blank OR an
    # all-disallowed value falls back to the derived title (never sends nothing).
    bezeichnung = _sanitize_desc(description) or _bezeichnung(doc)

    # Pre-upload duplicate guard: is this document ALREADY in Simba? The portal
    # forbids withdrawal, so surface a likely-existing copy and require an
    # explicit `force` to proceed. Best-effort (a check failure never blocks).
    if not force:
        existing = await _find_in_simba(
            mcp_manager,
            bezeichnung or Path(doc.filename or "").stem,
            category=category,
            type_=type_,
        )
        if existing:
            return {
                "success": False,
                "message": "already_in_simba",
                "existing": _summarize_transfer(existing[0]),
            }

    with open(doc.file_path, "rb") as f:
        content_base64 = base64.b64encode(f.read()).decode("ascii")

    # Claim BEFORE the irreversible upload — the mutual-exclusion window.
    if not await _claim_pending(db, proposal_id):
        return {"success": False, "message": "already_resolved"}

    file_entry: dict[str, str] = {"content_base64": content_base64, "filename": doc.filename}
    if bezeichnung:
        file_entry["description"] = bezeichnung
    tool_args: dict = {
        "category": category.strip(),
        "type": type_.strip(),
        "dry_run": False,
        "confirm": True,
        "files": [file_entry],
    }
    # Booking period (Buchungszeitraum) — validated; only forwarded when sane so
    # a bad value can't reach the portal. An omitted period lets the MCP fall back
    # to the current month/year (its documented default).
    if isinstance(month, int) and 1 <= month <= 12:
        tool_args["month"] = month
    if isinstance(year, int) and 2000 <= year <= 2100:
        tool_args["year"] = year
    try:
        # truncate=False: a truncated envelope would mangle the JSON result →
        # a landed upload misread as failed → a retry that double-uploads.
        result = await mcp_manager.execute_tool(
            "mcp.simba.upload_documents", tool_args, truncate=False
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"simba-ingest confirm: upload error for proposal {proposal_id}: {e}")
        await _revert_claim(db, proposal_id)
        return {"success": False, "message": f"upload error: {e}"}

    if not _landed(result):
        # Log the raw envelope so a "landed-but-misparsed" case is diagnosable
        # (vs. a genuine 0-transfer) — the two are otherwise indistinguishable.
        logger.error(
            f"simba-ingest confirm: proposal {proposal_id} not landed; raw={result!r}"
        )
        await _revert_claim(db, proposal_id)
        return {"success": False, "message": _failure_reason(result)}

    res = await db.execute(
        update(SimbaIngestProposal)
        .where(
            SimbaIngestProposal.id == proposal_id,
            SimbaIngestProposal.status == SIMBA_PROPOSAL_UPLOADING,
        )
        .values(
            status=SIMBA_PROPOSAL_UPLOADED,
            suggested_category=category.strip(),
            suggested_type=type_.strip(),
            resolved_at=_utcnow(),
            resolved_by_user_id=getattr(user, "id", None),
        )
    )
    await db.commit()
    # Push a content-free documents_changed(simba) so the owner's KB tabs flip the
    # Simba status icon without a reload. API-pod path, but publishes to Redis for
    # one uniform delivery route. Best-effort.
    try:
        from services.redis_client import get_redis
        from services.user_events import emit_documents_changed

        await emit_documents_changed(
            get_redis(), reason="simba",
            owner_user_id=getattr(p, "user_id", None), db=db, document=doc,
        )
    except Exception:  # noqa: BLE001 — never break a completed upload on an event
        pass
    if res.rowcount == 0:
        # The upload LANDED but the row left UPLOADING out from under us — the
        # terminal state is lost. Never a double upload (we held the claim), but
        # surface the inconsistency for reconciliation.
        logger.error(
            f"simba-ingest confirm: proposal {proposal_id} uploaded to Simba but "
            f"could not be marked UPLOADED (row no longer UPLOADING)"
        )
    return {"success": True, "message": f"An Simba übertragen: {category} / {type_}"}


def _inner(result) -> dict:
    if not result or not result.get("success"):
        return {}
    raw = result.get("message")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


async def _find_in_simba(
    mcp_manager, contains: str, category: str | None = None, type_: str | None = None
) -> list[dict]:
    """Best-effort duplicate check: does Simba already have a transfer matching
    this document? Narrows by the Suchbegriff (Bezeichnung/Dateiname) AND the
    Kategorie/Typ — all driven SERVER-SIDE by the MCP (≥v1.0.8), so the portal
    returns only matching rows (no full-grid scan). Returns the matching rows
    (empty on no match, blank query, or any error — the check never blocks a
    legitimate upload)."""
    if mcp_manager is None or not (contains or "").strip():
        return []
    args: dict = {"contains": contains.strip(), "limit": 20}
    if category and category.strip():
        args["category"] = category.strip()
    if type_ and type_.strip():
        args["type"] = type_.strip()
    try:
        res = await mcp_manager.execute_tool("mcp.simba.list_transfers", args, truncate=False)
    except Exception as e:  # noqa: BLE001 — the guard is advisory, never fatal
        logger.warning(f"simba-ingest: list_transfers check failed: {e}")
        return []
    rows = _inner(res).get("zeilen")
    return rows if isinstance(rows, list) else []


def _summarize_transfer(row: dict) -> str:
    """A short, human label for an existing Simba transfer row (column names vary,
    so probe the likely keys)."""
    if not isinstance(row, dict):
        return "vorhandener Eintrag"
    def _pick(*keys):
        for k in row:
            kl = str(k).lower()
            if any(w in kl for w in keys):
                v = str(row[k]).strip()
                if v:
                    return v
        return ""
    name = _pick("beschreibung", "bezeichnung", "dateiname", "datei", "name")
    period = _pick("monat", "jahr", "zeitraum")
    return " · ".join(x for x in (name, period) if x) or "vorhandener Eintrag"


def _landed(result) -> bool:
    if not result or not result.get("success"):
        return False
    inner = _inner(result)
    try:
        uebertragen = int(inner.get("uebertragen"))
    except (TypeError, ValueError):
        return False
    fehlgeschlagen = inner.get("fehlgeschlagen") or 0
    return uebertragen > 0 and not fehlgeschlagen


def _failure_reason(result) -> str:
    if not result or not result.get("success"):
        return (result or {}).get("message") or "Simba-Upload fehlgeschlagen"
    inner = _inner(result)
    errs = inner.get("fehler") or []
    if errs:
        e0 = errs[0]
        reason = e0.get("error") if isinstance(e0, dict) else str(e0)
        return reason or "Simba-Upload fehlgeschlagen"
    res0 = (inner.get("ergebnisse") or [{}])
    res0 = res0[0] if res0 else {}
    status = res0.get("status")
    if status:
        return f"HTTP {status}: {(res0.get('response') or '')[:200]}"
    return "Simba-Upload nicht angekommen"
