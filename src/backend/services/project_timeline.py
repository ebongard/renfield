"""Project timeline (Phase 4A).

Merges the time-ordered artifacts that belong to a Project into ONE chronological
feed for ``GET /api/projects/{id}/timeline``:

- **documents** ingested into the project's 1:1 KB (already project-scoped)
- **meetings** scoped via ``Meeting.project_id``
- **decisions** flattened out of a completed meeting's confirmed minutes JSON
  (no per-decision timestamp exists → stamped with the parent meeting's
  ``minutes_confirmed_at`` / date)
- **chat** conversations scoped via ``Conversation.project_id``

Shape mirrors the presence-analytics timeline (single ordered scan per source),
generalised to a multi-source merge: each source is queried independently, then
merge-sorted in Python (heterogeneous rows can't share one SQL projection
cleanly) newest-first and offset-sliced. Owner scoping + the feature gate live in
the route; this service only reads.
"""
from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Conversation, Document, Meeting, Project

# Per-source fetch cap (offset + limit, bounded) so a huge corpus can't pull
# everything into memory for a paged view. A timeline is a browse surface, not a
# bulk export; deep paging past this is intentionally not supported.
_SOURCE_CAP = 500


def _as_dt(value: datetime | date_cls | None) -> datetime | None:
    """Normalise a Date or DateTime to a comparable datetime (Date → midnight)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date_cls):
        return datetime(value.year, value.month, value.day)
    return None


def _doc_title(doc: Document) -> str:
    return (
        getattr(doc, "generated_title", None)
        or doc.title
        or doc.filename
        or f"Dokument {doc.id}"
    )


async def get_project_timeline(
    db: AsyncSession, project: Project, *, limit: int, offset: int
) -> list[dict]:
    """Return the project's merged timeline events, newest-first, offset-sliced."""
    fetch = min(offset + limit, _SOURCE_CAP)
    events: list[dict] = []

    # --- documents (project KB) -------------------------------------------- #
    if project.knowledge_base_id is not None:
        docs = (
            await db.execute(
                select(Document)
                .where(Document.knowledge_base_id == project.knowledge_base_id)
                .order_by(Document.created_at.desc())
                .limit(fetch)
            )
        ).scalars().all()
        for d in docs:
            ts = _as_dt(d.created_at)
            events.append({
                "_ts": ts,
                "kind": "document",
                "id": f"document-{d.id}",
                "ts": ts.isoformat() if ts else "",
                "title": _doc_title(d),
                "subtitle": d.status if d.status != "completed" else None,
                "document_id": d.id,
                "meeting_id": None,
                "conversation_session_id": None,
            })

    # --- meetings + their decisions ---------------------------------------- #
    meetings = (
        await db.execute(
            select(Meeting)
            .where(Meeting.project_id == project.id)
            .order_by(Meeting.created_at.desc())
            .limit(fetch)
        )
    ).scalars().all()
    for m in meetings:
        m_ts = _as_dt(m.date) or _as_dt(m.created_at)
        events.append({
            "_ts": m_ts,
            "kind": "meeting",
            "id": f"meeting-{m.id}",
            "ts": m_ts.isoformat() if m_ts else "",
            "title": m.title or f"Besprechung {m.id}",
            "subtitle": m.status if m.status != "completed" else None,
            "document_id": m.transcript_document_id,
            "meeting_id": m.id,
            "conversation_session_id": None,
        })
        # Flatten CONFIRMED decisions into their own events.
        if m.minutes_status == "confirmed" and isinstance(m.minutes, dict):
            d_ts = _as_dt(m.minutes_confirmed_at) or m_ts
            for idx, dec in enumerate(m.minutes.get("decisions") or []):
                text = (dec or {}).get("text") if isinstance(dec, dict) else None
                if not text:
                    continue
                events.append({
                    "_ts": d_ts,
                    "kind": "decision",
                    "id": f"decision-{m.id}-{idx}",
                    "ts": d_ts.isoformat() if d_ts else "",
                    "title": text,
                    "subtitle": (dec.get("made_by") or None),
                    "document_id": m.transcript_document_id,
                    "meeting_id": m.id,
                    "conversation_session_id": None,
                })

    # --- chat conversations ------------------------------------------------ #
    convs = (
        await db.execute(
            select(Conversation)
            .where(Conversation.project_id == project.id)
            .order_by(Conversation.updated_at.desc())
            .limit(fetch)
        )
    ).scalars().all()
    for c in convs:
        c_ts = _as_dt(c.updated_at) or _as_dt(c.created_at)
        summary = (c.summary or "").strip()
        events.append({
            "_ts": c_ts,
            "kind": "chat",
            "id": f"chat-{c.id}",
            "ts": c_ts.isoformat() if c_ts else "",
            "title": (summary[:120] if summary else f"Unterhaltung {c.id}"),
            "subtitle": None,
            "document_id": None,
            "meeting_id": None,
            "conversation_session_id": c.session_id,
        })

    # Merge-sort newest-first (undated rows sink to the bottom), then slice.
    events.sort(key=lambda e: (e["_ts"] is not None, e["_ts"] or datetime.min), reverse=True)
    window = events[offset:offset + limit]
    for e in window:
        e.pop("_ts", None)
    return window
