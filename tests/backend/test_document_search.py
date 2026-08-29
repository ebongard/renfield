"""Tests for the ranked hybrid document search (services/document_search.py) +
the GET /api/knowledge/documents?q= route branch.

The sqlite harness has no FTS/embeddings, so the fact + chunk signals no-op
(best-effort try/except) and the NAME signal exercises the ILIKE path. The
Postgres-only FTS ranking + circle-visibility gate are covered in
test_document_search_pg.py.
"""
import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Document
from services.document_search import search_documents


def _doc(db, filename, generated_title=None, title=None, created_at=None, status="completed"):
    d = Document(
        filename=filename, file_path=f"/tmp/{filename}", status=status,
        generated_title=generated_title, title=title,
    )
    if created_at is not None:
        d.created_at = created_at
    db.add(d)
    return d


@pytest.mark.backend
async def test_name_search_finds_by_generated_title(db_session: AsyncSession):
    for fn, gt in [
        ("a.pdf", "Rechnung Telekom"),
        ("b.pdf", "Invoice 10609009 from Arkadon dated 2026-01-30"),
        ("c.pdf", "Brief Finanzamt"),
    ]:
        _doc(db_session, fn, generated_title=gt)
    await db_session.commit()

    res = await search_documents(db_session, "Arkadon", asker_id=None, enforce_circles=False)
    assert [d.filename for d in res] == ["b.pdf"]


@pytest.mark.backend
async def test_name_search_matches_filename_and_title(db_session: AsyncSession):
    _doc(db_session, "muster.pdf", title="Ein Titel ohne Suchwort")
    _doc(db_session, "arkadon_2026.pdf")  # match on filename
    _doc(db_session, "x.pdf", title="Arkadon Vertrag")  # match on title
    await db_session.commit()

    res = await search_documents(db_session, "arkadon", asker_id=None, enforce_circles=False)
    names = {d.filename for d in res}
    assert names == {"arkadon_2026.pdf", "x.pdf"}


@pytest.mark.backend
async def test_empty_query_returns_empty(db_session: AsyncSession):
    _doc(db_session, "a.pdf", generated_title="Arkadon")
    await db_session.commit()
    assert await search_documents(db_session, "   ", asker_id=None, enforce_circles=False) == []


@pytest.mark.backend
async def test_reachability_beyond_recency_window(db_session: AsyncSession):
    """The bug: an old document past the recency window is unreachable by the
    plain list but IS found by search."""
    now = datetime.datetime.utcnow()
    # target is the OLDEST doc
    _doc(db_session, "target.pdf", generated_title="Invoice Arkadon",
         created_at=now - datetime.timedelta(days=400))
    for i in range(5):  # newer filler docs
        _doc(db_session, f"filler{i}.pdf", generated_title=f"Rechnung {i}",
             created_at=now - datetime.timedelta(days=i))
    await db_session.commit()

    # A recency list capped at 3 would return the 3 newest fillers, NOT the target.
    res = await search_documents(db_session, "Arkadon", asker_id=None, enforce_circles=False, limit=3)
    assert [d.filename for d in res] == ["target.pdf"]  # search reaches it regardless


@pytest.mark.backend
async def test_route_q_searches_documents(async_client: AsyncClient, db_session: AsyncSession):
    _doc(db_session, "a.pdf", generated_title="Rechnung Telekom")
    _doc(db_session, "b.pdf", generated_title="Invoice Arkadon 2026")
    await db_session.commit()

    resp = await async_client.get("/api/knowledge/documents", params={"q": "Arkadon"})
    assert resp.status_code == 200
    names = [x["display_name"] for x in resp.json()]
    assert any("Arkadon" in (n or "") for n in names)
    assert all("Telekom" not in (n or "") for n in names)


@pytest.mark.backend
async def test_route_without_q_is_recency_list(async_client: AsyncClient, db_session: AsyncSession):
    _doc(db_session, "a.pdf", generated_title="Doc A")
    await db_session.commit()
    resp = await async_client.get("/api/knowledge/documents")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
