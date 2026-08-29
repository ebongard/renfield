"""`internal.find_duplicate_documents` — KB near-duplicate detection chat tool (#1170).

Closes the scope gap behind "sind Duplikate in der Wissensbasis?": that question
used to route to `internal.paperless_dedupe`, which only inspects Paperless and
can't see two KB documents that share content but never became two Paperless docs.

This tool runs the KB near-duplicate DETECTOR (services/document_dedupe_service.py)
for the asker, which finds documents that byte-hash ingest dedup misses (different
file_hash) yet share a document-unique identifier (e.g. the same invoice number),
and surfaces each pair for owner review on /brain/review. It is NON-destructive —
it only proposes; the owner approves/rejects (and chooses supersede vs delete) in
the review UI. Nothing is ever auto-deleted.

Registered like the other KB-maintenance tools: the tool schema is merged into the
agent tool set by `agent_tools._register_internal_tools`, and the handler is
dispatched as a special case in `action_executor` (which injects the authenticated
`user_id`, so the scan only ever covers the caller's own documents).
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import select

from models.database import (
    DOC_DUP_PROPOSAL_PENDING,
    Document,
    DocumentDuplicateProposal,
)
from services.database import AsyncSessionLocal
from services.document_dedupe_service import DocumentDedupeService
from utils.config import settings

FIND_DUPLICATE_DOCUMENTS_TOOL: dict = {
    "internal.find_duplicate_documents": {
        "description": (
            "Find NEAR-DUPLICATE documents in the KNOWLEDGE BASE (not Paperless). "
            "Use for 'gibt es Dubletten/Duplikate in der Wissensbasis?', 'sind "
            "Dokumente doppelt vorhanden?', 'finde doppelte Dokumente'. This "
            "detects two documents that are the SAME document but were stored twice "
            "with different bytes (a re-scan, a re-export, the same invoice from two "
            "sources) — which the exact-hash ingest dedup and the Paperless dedupe "
            "both miss. It matches documents sharing a unique identifier (e.g. the "
            "same invoice/order number). It only PROPOSES pairs for review on "
            "/brain/review — it never deletes anything. NOTE: this is the KNOWLEDGE-"
            "BASE scope; 'räum die Duplikate in Paperless auf' is the separate "
            "paperless_dedupe tool."
        ),
        "parameters": {},
    }
}


def _display_name(doc: Document | None) -> str:
    if doc is None:
        return "?"
    return doc.generated_title or doc.title or doc.filename or f"Dokument {doc.id}"


async def find_duplicate_documents(parameters: dict, user_id: int | None = None) -> dict:
    """Run the KB near-duplicate detector for the caller and report the pairs."""
    if not settings.document_dedupe_enabled:
        return {
            "success": True,
            "action_taken": False,
            "message": (
                "Die Dubletten-Erkennung für die Wissensbasis ist derzeit deaktiviert "
                "(document_dedupe_enabled). Für Paperless-Duplikate gibt es die "
                "separate Aufräum-Funktion."
            ),
            "data": {"enabled": False, "pairs": []},
        }

    try:
        async with AsyncSessionLocal() as db:
            report = await DocumentDedupeService(db).run_for_user(user_id)

            # Read back the caller's pending pairs (incl. ones from earlier runs)
            # to list them by name.
            q = (
                select(DocumentDuplicateProposal)
                .where(DocumentDuplicateProposal.status == DOC_DUP_PROPOSAL_PENDING)
            )
            if settings.auth_enabled:
                q = q.where(DocumentDuplicateProposal.user_id == user_id)
            q = q.order_by(DocumentDuplicateProposal.id.desc()).limit(50)
            proposals = (await db.execute(q)).scalars().all()

            pairs = []
            for p in proposals:
                da = (await db.execute(select(Document).where(Document.id == p.document_a_id))).scalar_one_or_none()
                db_doc = (await db.execute(select(Document).where(Document.id == p.document_b_id))).scalar_one_or_none()
                pairs.append({
                    "proposal_id": p.id,
                    "document_a": {"id": p.document_a_id, "name": _display_name(da)},
                    "document_b": {"id": p.document_b_id, "name": _display_name(db_doc)},
                    "suggested_survivor_id": p.suggested_survivor_id,
                    "shared_key": p.shared_key,
                })
    except Exception as exc:  # noqa: BLE001 — best-effort chat tool, never crash the turn
        logger.error(f"find_duplicate_documents failed: {exc}")
        return {
            "success": False,
            "action_taken": False,
            "message": "Die Dubletten-Prüfung ist fehlgeschlagen.",
            "data": {"pairs": []},
        }

    n = len(pairs)
    if n == 0:
        msg = "Ich habe keine doppelten Dokumente in der Wissensbasis gefunden."
    else:
        lines = [
            f"• „{pp['document_a']['name']}“ ↔ „{pp['document_b']['name']}“ "
            f"(gemeinsame Kennung {pp['shared_key']})"
            for pp in pairs[:10]
        ]
        more = f"\n… und {n - 10} weitere" if n > 10 else ""
        newly = f" ({report.proposed} neu erkannt)" if report.proposed else ""
        msg = (
            f"Ich habe {n} mögliche Dubletten-Paare in der Wissensbasis gefunden{newly}. "
            "Du kannst sie unter „Prüfen“ (/brain/review) bestätigen oder verwerfen — "
            "dabei wählst du je Paar, ob das doppelte Dokument nur ausgeblendet "
            "(wiederherstellbar) oder gelöscht wird. Es wird nichts automatisch "
            "gelöscht.\n" + "\n".join(lines) + more
        )

    return {
        "success": True,
        "action_taken": report.proposed > 0,
        "message": msg,
        "data": {
            "enabled": True,
            "candidates": report.candidates,
            "newly_proposed": report.proposed,
            "pending_pairs": n,
            "pairs": pairs,
        },
    }
