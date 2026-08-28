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

from models.database import (
    FOLDER_INGEST_SOURCE,
    SIMBA_PROPOSAL_PENDING,
    SIMBA_PROPOSAL_REJECTED,
    SIMBA_PROPOSAL_UPLOADED,
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
        except Exception as e:  # noqa: BLE001 — unique-pending race is benign
            await db.rollback()
            logger.info(f"simba-ingest: proposal not created for doc {document_id}: {e}")


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


async def confirm(db, proposal_id: int, category: str, type_: str, user, mcp_manager) -> dict:
    """Confirm a pending proposal → REAL upload to Simba, then mark uploaded.

    Returns {"success": bool, "message": str}. The proposal is only marked
    uploaded when the document actually landed (uebertragen>0).
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

    tool_args = {
        "category": category.strip(),
        "type": type_.strip(),
        "dry_run": False,
        "confirm": True,
        "files": [{"content_base64": content_base64, "filename": doc.filename}],
    }
    try:
        result = await mcp_manager.execute_tool("mcp.simba.upload_documents", tool_args)
    except Exception as e:  # noqa: BLE001
        logger.error(f"simba-ingest confirm: upload error for proposal {proposal_id}: {e}")
        return {"success": False, "message": f"upload error: {e}"}

    if not _landed(result):
        return {"success": False, "message": _failure_reason(result)}

    await db.execute(
        update(SimbaIngestProposal)
        .where(
            SimbaIngestProposal.id == proposal_id,
            SimbaIngestProposal.status == SIMBA_PROPOSAL_PENDING,
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
        return errs[0].get("error") if isinstance(errs[0], dict) else str(errs[0])
    res0 = (inner.get("ergebnisse") or [{}])
    res0 = res0[0] if res0 else {}
    status = res0.get("status")
    if status:
        return f"HTTP {status}: {(res0.get('response') or '')[:200]}"
    return "Simba-Upload nicht angekommen"
