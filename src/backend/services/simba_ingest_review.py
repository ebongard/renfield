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
    Document,
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
                    user_id=doc.user_id if doc.user_id is not None else user_id,
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


async def list_pending(db, user) -> list[SimbaIngestProposal]:
    q = select(SimbaIngestProposal).where(
        SimbaIngestProposal.status == SIMBA_PROPOSAL_PENDING
    ).order_by(SimbaIngestProposal.id.desc())
    rows = list((await db.execute(q)).scalars().all())
    return [p for p in rows if _owns(p, user)]


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


async def confirm(db, proposal_id: int, category: str, type_: str, user, mcp_manager) -> dict:
    """Confirm a pending proposal → REAL upload to Simba, then mark uploaded.

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

    with open(doc.file_path, "rb") as f:
        content_base64 = base64.b64encode(f.read()).decode("ascii")

    # Claim BEFORE the irreversible upload — the mutual-exclusion window.
    if not await _claim_pending(db, proposal_id):
        return {"success": False, "message": "already_resolved"}

    tool_args = {
        "category": category.strip(),
        "type": type_.strip(),
        "dry_run": False,
        "confirm": True,
        "files": [{"content_base64": content_base64, "filename": doc.filename}],
    }
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
