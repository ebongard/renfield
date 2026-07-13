"""Backend tests for document circle-tier control — HTTP-validation cases.

These exercise the tier endpoints' request validation + response shape, which
are backend-agnostic and run on the default (SQLite) ``async_client`` fixture:
- ``DocumentResponse`` exposes ``circle_tier`` + ``atom_id``.
- tier out of range → 422; missing document → 404.

The tier-MUTATING cases (PATCH/bulk cascade, null-atom mint) go through
``AtomService.update_tier``, whose ``id::text`` cast is Postgres-only, so they
live in ``test_document_tier_pg.py`` against real Postgres.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Document, DocumentChunk, KnowledgeBase, User
from services.atom_service import AtomService


async def _make_doc(
    db: AsyncSession,
    kb: KnowledgeBase,
    owner: User,
    *,
    tier: int = 0,
    chunks: int = 2,
    with_atom: bool = True,
) -> Document:
    """Create a completed document (+ chunks), optionally with a kb_document atom."""
    doc = Document(
        filename="tier_doc.pdf",
        title="Tier Doc",
        file_path="/tmp/tier_doc.pdf",
        file_type="pdf",
        file_size=42,
        status="completed",
        chunk_count=chunks,
        knowledge_base_id=kb.id,
        circle_tier=tier,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    for i in range(chunks):
        db.add(DocumentChunk(
            document_id=doc.id,
            content=f"chunk {i} content",
            chunk_index=i,
            chunk_type="paragraph",
            circle_tier=tier,
        ))
    await db.commit()

    if with_atom:
        atom_id = await AtomService(db).create_with_source(
            atom_type="kb_document",
            owner_user_id=owner.id,
            tier=tier,
            source_id=doc.id,
        )
        doc.atom_id = atom_id
        await db.commit()
        await db.refresh(doc)
    return doc


@pytest.mark.database
async def test_tier_out_of_range_rejected(
    db_session: AsyncSession, async_client, test_knowledge_base_with_owner, test_user
):
    doc = await _make_doc(db_session, test_knowledge_base_with_owner, test_user)
    resp = await async_client.patch(
        f"/api/knowledge/documents/{doc.id}/tier", json={"tier": 5}
    )
    assert resp.status_code == 422


@pytest.mark.database
async def test_tier_missing_document_404(async_client):
    resp = await async_client.patch(
        "/api/knowledge/documents/999999/tier", json={"tier": 4}
    )
    assert resp.status_code == 404


@pytest.mark.database
async def test_document_response_exposes_tier(
    db_session: AsyncSession, async_client, test_knowledge_base_with_owner, test_user
):
    doc = await _make_doc(db_session, test_knowledge_base_with_owner, test_user, tier=2)
    resp = await async_client.get(f"/api/knowledge/documents/{doc.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["circle_tier"] == 2
    assert body["atom_id"]
