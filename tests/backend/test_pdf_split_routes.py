"""Route + service tests for the PDF-split review flow (PR2).

Integration-style via async_client + the sqlite db_session (the ORM declares
the pending-partial-unique with sqlite_where, so create_all enforces it here
too). Queue/worker/notification side effects are patched — approve/reject must
only persist state + enqueue; the split itself always runs in the worker.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import services.pdf_split_proposals as psp
from models.database import (
    DOC_STATUS_PENDING,
    DOC_STATUS_SPLIT_PENDING,
    DOC_STATUS_SPLIT_REVIEW,
    PDF_SPLIT_PROPOSAL_APPROVED,
    PDF_SPLIT_PROPOSAL_PENDING,
    PDF_SPLIT_PROPOSAL_REJECTED,
    Document,
    PdfSplitProposal,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _pieces_json(*ranges, conf=0.6):
    return [
        {
            "start_page": s,
            "end_page": e,
            "title": f"Doc {s}-{e}",
            "doc_type": "letter",
            "confidence": conf,
        }
        for s, e in ranges
    ]


async def _seed(db: AsyncSession, *, page_count=5, status=DOC_STATUS_SPLIT_REVIEW):
    doc = Document(
        filename="stapel.pdf",
        file_path="/uploads/x_stapel.pdf",
        file_hash=f"h_{id(object())}",
        status=status,
        circle_tier=0,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    row = PdfSplitProposal(
        document_id=doc.id,
        status=PDF_SPLIT_PROPOSAL_PENDING,
        proposal=_pieces_json((1, 2), (3, 5)),
        page_signals=[{"page": p, "snippet": f"Seite {p}", "quality_ok": True} for p in range(1, 6)],
        page_count=page_count,
        overall_confidence=0.6,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return doc, row


@pytest.fixture
def _enabled(monkeypatch):
    from utils.config import settings

    monkeypatch.setattr(settings, "pdf_split_enabled", True)
    # approve/reject enqueue via the proposals service; keep it hermetic
    queue = MagicMock()
    queue.enqueue = AsyncMock()
    monkeypatch.setattr(psp, "DocumentTaskQueue", MagicMock(return_value=queue))
    monkeypatch.setattr(psp, "get_redis", MagicMock())
    monkeypatch.setattr(
        "api.routes.pdf_split.document_worker_is_alive", AsyncMock(return_value=True)
    )
    return queue


async def test_routes_404_when_flag_off(async_client: AsyncClient):
    resp = await async_client.get("/api/pdf-split/proposals")
    assert resp.status_code == 404


async def test_list_and_detail(async_client: AsyncClient, db_session, _enabled):
    doc, row = await _seed(db_session)

    resp = await async_client.get("/api/pdf-split/proposals")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["proposals"][0]["document_filename"] == "stapel.pdf"
    assert len(data["proposals"][0]["documents"]) == 2

    detail = await async_client.get(f"/api/pdf-split/proposals/{row.id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["page_count"] == 5
    assert len(body["page_signals"]) == 5

    missing = await async_client.get("/api/pdf-split/proposals/999999")
    assert missing.status_code == 404


async def test_approve_as_is_enqueues_worker_execution(
    async_client: AsyncClient, db_session, _enabled
):
    doc, row = await _seed(db_session)

    resp = await async_client.post(f"/api/pdf-split/proposals/{row.id}/approve")

    assert resp.status_code == 200
    await db_session.refresh(row)
    await db_session.refresh(doc)
    assert row.status == PDF_SPLIT_PROPOSAL_APPROVED
    assert doc.status == DOC_STATUS_SPLIT_PENDING  # worker resumes via stored plan
    _enabled.enqueue.assert_awaited_once()
    params = _enabled.enqueue.await_args.args[0]
    assert params["document_id"] == doc.id
    assert "skip_split" not in params


async def test_approve_with_edited_ranges(async_client: AsyncClient, db_session, _enabled):
    doc, row = await _seed(db_session)

    resp = await async_client.post(
        f"/api/pdf-split/proposals/{row.id}/approve",
        json={"documents": _pieces_json((1, 3), (4, 5))},
    )

    assert resp.status_code == 200
    await db_session.refresh(row)
    assert [(p["start_page"], p["end_page"]) for p in row.proposal] == [(1, 3), (4, 5)]
    assert row.status == PDF_SPLIT_PROPOSAL_APPROVED


async def test_approve_non_covering_ranges_is_422(
    async_client: AsyncClient, db_session, _enabled
):
    doc, row = await _seed(db_session)

    resp = await async_client.post(
        f"/api/pdf-split/proposals/{row.id}/approve",
        json={"documents": _pieces_json((1, 2), (4, 5))},  # page 3 uncovered
    )

    assert resp.status_code == 422
    await db_session.refresh(row)
    assert row.status == PDF_SPLIT_PROPOSAL_PENDING  # unchanged
    _enabled.enqueue.assert_not_called()


async def test_reject_reenqueues_with_skip_split(
    async_client: AsyncClient, db_session, _enabled
):
    doc, row = await _seed(db_session)

    resp = await async_client.post(f"/api/pdf-split/proposals/{row.id}/reject")

    assert resp.status_code == 200
    await db_session.refresh(row)
    await db_session.refresh(doc)
    assert row.status == PDF_SPLIT_PROPOSAL_REJECTED
    assert doc.status == DOC_STATUS_PENDING  # un-parked for normal ingest
    params = _enabled.enqueue.await_args.args[0]
    assert params["skip_split"] is True


async def test_cross_resolution_conflicts_409(
    async_client: AsyncClient, db_session, _enabled
):
    """Approving a REJECTED proposal (or rejecting an APPROVED one) is a
    genuine conflict — the MATCHING action instead retries idempotently."""
    doc, row = await _seed(db_session)
    row.status = PDF_SPLIT_PROPOSAL_REJECTED
    await db_session.commit()

    resp = await async_client.post(f"/api/pdf-split/proposals/{row.id}/approve")
    assert resp.status_code == 409


async def test_approve_retry_is_idempotent_re_enqueue(
    async_client: AsyncClient, db_session, _enabled
):
    """The Redis-blip recovery route: the proposal is already APPROVED and the
    parent still parked (the original enqueue was lost) — retrying the approve
    re-enqueues WITHOUT state change instead of 409ing (which would strand the
    doc in 'split_pending' forever)."""
    doc, row = await _seed(db_session, status=DOC_STATUS_SPLIT_PENDING)
    row.status = PDF_SPLIT_PROPOSAL_APPROVED
    await db_session.commit()

    resp = await async_client.post(f"/api/pdf-split/proposals/{row.id}/approve")

    assert resp.status_code == 200
    _enabled.enqueue.assert_awaited_once()
    assert _enabled.enqueue.await_args.args[0]["document_id"] == doc.id


async def test_reject_retry_is_idempotent_re_enqueue(
    async_client: AsyncClient, db_session, _enabled
):
    doc, row = await _seed(db_session, status=DOC_STATUS_PENDING)
    row.status = PDF_SPLIT_PROPOSAL_REJECTED
    await db_session.commit()

    resp = await async_client.post(f"/api/pdf-split/proposals/{row.id}/reject")

    assert resp.status_code == 200
    params = _enabled.enqueue.await_args.args[0]
    assert params["skip_split"] is True


async def test_worker_dead_is_503(async_client: AsyncClient, db_session, _enabled, monkeypatch):
    doc, row = await _seed(db_session)
    monkeypatch.setattr(
        "api.routes.pdf_split.document_worker_is_alive", AsyncMock(return_value=False)
    )

    resp = await async_client.post(f"/api/pdf-split/proposals/{row.id}/approve")
    assert resp.status_code == 503
    await db_session.refresh(row)
    assert row.status == PDF_SPLIT_PROPOSAL_PENDING


async def test_page_render_missing_file_is_404(
    async_client: AsyncClient, db_session, _enabled
):
    doc, row = await _seed(db_session)  # file_path doesn't exist on disk
    resp = await async_client.get(f"/api/pdf-split/proposals/{row.id}/pages/1")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# create_review_proposal (service level, real session)
# ---------------------------------------------------------------------------

def _verdict(*ranges, conf=0.6):
    from services.pdf_split_detector import PageSignal, SplitPiece, SplitVerdict

    pieces = [
        SplitPiece(start_page=s, end_page=e, title=f"D{s}", doc_type="", confidence=conf)
        for s, e in ranges
    ]
    last = ranges[-1][1]
    return SplitVerdict(
        kind="multi",
        pieces=pieces,
        page_signals=[PageSignal(page=p, text="x", quality_ok=True) for p in range(1, last + 1)],
    )


async def test_create_review_proposal_parks_parent_and_notifies(db_session, monkeypatch):
    notify = AsyncMock()
    monkeypatch.setattr(psp, "_notify_owner", notify)
    doc = Document(
        filename="s.pdf", file_path="/uploads/s.pdf", file_hash="h_crp",
        status=DOC_STATUS_PENDING, circle_tier=0,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    row = await psp.create_review_proposal(db_session, doc, _verdict((1, 2), (3, 4)), None)

    assert row.status == PDF_SPLIT_PROPOSAL_PENDING
    assert row.page_count == 4
    assert doc.status == DOC_STATUS_SPLIT_REVIEW
    notify.assert_awaited_once()


async def test_create_review_proposal_refreshes_existing_pending(db_session, monkeypatch):
    """A re-detection (e.g. after REINGEST) must REFRESH the pending row, not
    violate the one-pending-per-document partial unique."""
    monkeypatch.setattr(psp, "_notify_owner", AsyncMock())
    doc = Document(
        filename="s2.pdf", file_path="/uploads/s2.pdf", file_hash="h_crp2",
        status=DOC_STATUS_PENDING, circle_tier=0,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    first = await psp.create_review_proposal(db_session, doc, _verdict((1, 2), (3, 4)), None)
    second = await psp.create_review_proposal(db_session, doc, _verdict((1, 1), (2, 4)), None)

    assert second.id == first.id  # refreshed in place
    assert [(p["start_page"], p["end_page"]) for p in second.proposal] == [(1, 1), (2, 4)]


async def test_notify_fires_only_for_new_proposal(db_session, monkeypatch):
    """A refresh of the pending row (re-detection) must NOT re-fire the
    'PDF-Prüfung wartet' notification — the 60s NotificationService dedup
    window cannot cover an hours-later refresh."""
    notify = AsyncMock()
    monkeypatch.setattr(psp, "_notify_owner", notify)
    doc = Document(
        filename="n.pdf", file_path="/uploads/n.pdf", file_hash="h_notify",
        status=DOC_STATUS_PENDING, circle_tier=0,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    await psp.create_review_proposal(db_session, doc, _verdict((1, 1), (2, 2)), None)
    await psp.create_review_proposal(db_session, doc, _verdict((1, 2), (3, 3)), None)

    notify.assert_awaited_once()


async def test_rejected_proposal_is_durable_treat_as_single(db_session, monkeypatch):
    """has_rejected_proposal backs the detection-side guard: after an owner
    reject, a later plain task must not re-park the document."""
    monkeypatch.setattr(psp, "_notify_owner", AsyncMock())
    doc = Document(
        filename="r.pdf", file_path="/uploads/r.pdf", file_hash="h_rejdur",
        status=DOC_STATUS_PENDING, circle_tier=0,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)

    assert await psp.has_rejected_proposal(db_session, doc.id) is False
    row = await psp.create_review_proposal(db_session, doc, _verdict((1, 1), (2, 2)), None)

    queue = MagicMock()
    queue.enqueue = AsyncMock()
    monkeypatch.setattr(psp, "DocumentTaskQueue", MagicMock(return_value=queue))
    monkeypatch.setattr(psp, "get_redis", MagicMock())
    await psp.reject_proposal(db_session, row, resolved_by=None)

    assert await psp.has_rejected_proposal(db_session, doc.id) is True


async def test_ownerless_proposal_visible_to_admin_only(db_session, monkeypatch):
    """Under AUTH_ENABLED an ownerless proposal (NULL user_id) must be
    resolvable by an admin — otherwise the parked parent strands invisibly."""
    from fastapi import HTTPException

    from api.routes.pdf_split import _owned_proposal
    from utils.config import settings as cfg

    doc, row = await _seed(db_session)
    row.user_id = None
    await db_session.commit()
    monkeypatch.setattr(cfg, "auth_enabled", True)

    class _U:
        def __init__(self, uid, perms):
            self.id = uid
            self._perms = perms

        def get_permissions(self):
            return self._perms

    admin = _U(1, ["admin"])
    plain = _U(2, ["kb.own"])

    got = await _owned_proposal(db_session, row.id, admin)
    assert got.id == row.id

    with pytest.raises(HTTPException) as exc:
        await _owned_proposal(db_session, row.id, plain)
    assert exc.value.status_code == 404
